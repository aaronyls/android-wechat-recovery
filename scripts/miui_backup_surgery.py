#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S8-a MIUI 备份 tar 手术。

流式逐条搬运原始 tar 条目，替换 EnMicroMsg.db/.sm/.li/.ini，
跳过 corrupted/、WAL/SHM、FTS5，输出新的 .bak。

【P1】默认从**本地文件**读取（推荐）。仅在不得已时才用 --from-adb 走手机流。
【格式铁律】保持 GNU tar（ustar\\0）、保留原 UID/GID、保留 GNU long name(L)、
           改 size 后重算 checksum（148-155 位按空格计算）。
           绝不能用 Python tarfile 重新打包——它输出 POSIX 格式且会改 uid/gid。

用法:
    python miui_backup_surgery.py --src wechat_orig.bak --out wechat_final.bak \
        --db EnMicroMsg_recovered.db --sm EnMicroMsg_recovered.db.sm \
        --li EnMicroMsg_corrupted.db.li --ini EnMicroMsg_corrupted.db.ini

    # 不得已从手机流式读取（纯 tar，无 MIUI 头）
    python miui_backup_surgery.py --from-adb --adb adb \
        --remote /sdcard/temp_bak/wechat.tar --out wechat_final.bak --db ... --sm ...
"""
import argparse
import os
import subprocess
import sys
import time

CHUNK = 4 * 1024 * 1024

# 66 字节 MIUI 头部（输入为纯 tar 时需自行补写）
MIUI_HEADER = (
    b'MIUI BACKUP\n'
    b'2\n'
    b'com.tencent.mm \xe5\xbe\xae\xe4\xbf\xa1\n'
    b'102\n0\n'
    b'ANDROID BACKUP\n'
    b'5\n0\nnone\n'
)
assert len(MIUI_HEADER) == 66


def compute_checksum(header):
    """tar 头校验和：chksum 字段(148-155)按空格 0x20 计算"""
    chk = 0
    for i in range(512):
        chk += 0x20 if 148 <= i < 156 else header[i]
    return chk


def update_header_size(header, new_size):
    """改 size 字段并重算 checksum，其余字节（含 uid/gid/magic）原封不动"""
    h = bytearray(header)
    h[124:136] = f'{new_size:011o}\0'.encode('ascii')
    chk = compute_checksum(h)
    h[148:156] = f'{chk:06o}\0 '.encode('ascii')
    return bytes(h)


def pad_size(size):
    return size if size % 512 == 0 else size + (512 - size % 512)


def human(n):
    return f'{n/1024**3:.2f} GB' if n >= 1024**3 else f'{n/1024**2:.1f} MB'


def read_exact(src, n):
    """从流中精确读 n 字节（ADB 管道可能短读）"""
    buf = b''
    while len(buf) < n:
        c = src.read(n - len(buf))
        if not c:
            break
        buf += c
    return buf


def copy_stream(src, out, size, counter):
    remaining = pad_size(size)
    while remaining > 0:
        c = src.read(min(remaining, CHUNK))
        if not c:
            break
        out.write(c)
        counter[0] += len(c)
        remaining -= len(c)


def skip_stream(src, size):
    remaining = pad_size(size)
    while remaining > 0:
        c = src.read(min(remaining, CHUNK))
        if not c:
            break
        remaining -= len(c)


def write_local(out, path, counter):
    written = 0
    with open(path, 'rb') as f:
        while True:
            c = f.read(CHUNK)
            if not c:
                break
            out.write(c)
            written += len(c)
    pad = pad_size(written) - written
    if pad:
        out.write(b'\0' * pad)
    counter[0] += pad_size(written)
    return written


def surgery(src, out_path, repl, has_miui_header, expected_total=0):
    """
    src: 可读流；repl: {'db':path,'sm':path,'li':path,'ini':path}
    has_miui_header: 输入流是否已包含 66 字节 MIUI 头
    """
    sizes = {k: os.path.getsize(v) for k, v in repl.items() if v}
    print('\n[替换文件]')
    for k, v in repl.items():
        if v:
            print(f'  {k:<4}: {os.path.basename(v):<34} {human(sizes[k])}')

    if os.path.exists(out_path):
        os.remove(out_path)

    total = [0]
    copied = replaced = skipped = 0
    start = time.time()
    last_report = start

    with open(out_path, 'wb') as out:
        if has_miui_header:
            hdr = read_exact(src, 66)
            if not hdr.startswith(b'MIUI BACKUP'):
                print(f'[错误] 输入声明含 MIUI 头，但前 11 字节是 {hdr[:11]!r}')
                return None
            out.write(hdr)
            total[0] += 66
            print('\n[头部] 原样搬运 66 字节 MIUI 头')
        else:
            out.write(MIUI_HEADER)
            total[0] += 66
            print('\n[头部] 输入为纯 tar，已补写 66 字节 MIUI 头')

        print('[开始逐条处理 tar 条目]')
        while True:
            header = read_exact(src, 512)
            if len(header) < 512:
                break
            if header == b'\0' * 512:
                out.write(header)
                total[0] += 512
                nxt = read_exact(src, 512)
                if nxt:
                    out.write(nxt)
                    total[0] += len(nxt)
                break

            name = header[0:100].rstrip(b'\x00').decode('utf-8', errors='replace')
            szs = header[124:136].rstrip(b'\x00 ').decode('ascii', errors='replace')
            try:
                size = int(szs, 8) if szs else 0
            except ValueError:
                size = 0
            tf = chr(header[156]) if header[156] else '0'

            # GNU long name 条目：必须原样保留
            if tf == 'L':
                out.write(header)
                total[0] += 512
                copy_stream(src, out, size, total)
                copied += 1
                continue

            in_corrupted = '/corrupted/' in name
            kind = None
            if not in_corrupted:
                if name.endswith('/EnMicroMsg.db'):
                    kind = 'db'
                elif name.endswith('/EnMicroMsg.db.sm'):
                    kind = 'sm'
                elif name.endswith('/EnMicroMsg.db.li'):
                    kind = 'li'
                elif name.endswith('/EnMicroMsg.db.ini'):
                    kind = 'ini'

            drop = (in_corrupted or name.endswith('.db-wal') or name.endswith('.db-shm')
                    or 'FTS5' in name or 'fts5' in name)

            if kind and repl.get(kind):
                out.write(update_header_size(header, sizes[kind]))
                total[0] += 512
                write_local(out, repl[kind], total)
                skip_stream(src, size)
                replaced += 1
                print(f'  REPLACE {kind.upper():<4} {name}')
                print(f'          {size} -> {sizes[kind]} bytes')
            elif drop:
                skip_stream(src, size)
                skipped += 1
            else:
                out.write(header)
                total[0] += 512
                copy_stream(src, out, size, total)
                copied += 1

            # P7 进度上报：每 30 秒一次
            if time.time() - last_report > 30:
                el = time.time() - start
                pct = f'{total[0]/expected_total*100:.0f}%' if expected_total else '--'
                print(f'  ... 已写 {human(total[0])} ({pct})  条目 {copied}/{replaced}/{skipped}'
                      f'  {total[0]/el/1024**2:.0f} MB/s  {el/60:.1f} min')
                last_report = time.time()

    el = time.time() - start
    out_size = os.path.getsize(out_path)
    print('\n' + '=' * 62)
    print('tar 手术完成')
    print('=' * 62)
    print(f'  复制条目 : {copied}')
    print(f'  替换条目 : {replaced}   （应为 4：db/sm/li/ini）')
    print(f'  跳过条目 : {skipped}   （corrupted/ + WAL/SHM + FTS5）')
    print(f'  输出文件 : {out_path}')
    print(f'  输出大小 : {out_size:,} bytes ({human(out_size)})')
    print(f'  耗时     : {el/60:.1f} 分钟')
    if replaced < 4:
        print(f'\n  ⚠ 替换数 {replaced} < 4，请检查 tar 内路径是否与预期一致')
    return out_size


def validate(path):
    """快速校验输出文件的格式合法性"""
    print('\n[输出校验]')
    ok = True
    with open(path, 'rb') as f:
        head = f.read(66)
        if head.startswith(b'MIUI BACKUP'):
            print('  ✓ MIUI 头部存在')
        else:
            print('  ✗ MIUI 头部缺失'); ok = False
        first = f.read(512)
        magic = first[257:263]
        if magic == b'ustar\x00':
            print(f'  ✓ GNU tar 格式 (magic={magic!r})')
        else:
            print(f'  ✗ tar magic 异常: {magic!r}  期望 b"ustar\\x00"'); ok = False
        uid = first[108:116].rstrip(b'\x00 ').decode('ascii', errors='replace')
        gid = first[116:124].rstrip(b'\x00 ').decode('ascii', errors='replace')
        print(f'  · 首条目 uid={uid} gid={gid}（应与原始 tar 一致）')
        f.seek(-1024, os.SEEK_END)
        if f.read(1024) == b'\0' * 1024:
            print('  ✓ end-of-archive 标记完整')
        else:
            print('  ✗ 末尾缺少两个 512 字节全零块'); ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', help='本地原始 .bak 或 .tar（推荐）')
    ap.add_argument('--from-adb', action='store_true', help='从手机流式读取（不推荐，违反 P1）')
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    ap.add_argument('--remote', help='--from-adb 时的手机端路径')
    ap.add_argument('--out', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--sm', required=True)
    ap.add_argument('--li', default=None)
    ap.add_argument('--ini', default=None)
    args = ap.parse_args()

    print('=' * 62)
    print('S8-a · MIUI 备份 tar 手术')
    print('=' * 62)

    repl = {'db': args.db, 'sm': args.sm, 'li': args.li, 'ini': args.ini}
    for k, v in repl.items():
        if v and not os.path.exists(v):
            print(f'[错误] 替换文件不存在: {v}')
            return 1

    if args.from_adb:
        if not args.remote:
            print('[错误] --from-adb 需配合 --remote')
            return 1
        print('\n⚠ 正在从手机流式读取，违反 P1（电脑端闭环）。')
        print('  仅当本地磁盘放不下原始备份时才应这样做。')
        cmd = [args.adb] + (['-s', args.serial] if args.serial else []) + \
              ['exec-out', f'cat "{args.remote}"']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=CHUNK)
        src, has_hdr, expect = proc.stdout, False, 0
        print(f'  源: {args.remote}（按纯 tar 处理，将自行补写 MIUI 头）')
        try:
            res = surgery(src, args.out, repl, has_hdr, expect)
        finally:
            proc.stdout.close()
            proc.wait()
    else:
        if not args.src or not os.path.exists(args.src):
            print(f'[错误] 源文件不存在: {args.src}')
            return 1
        with open(args.src, 'rb') as f:
            probe = f.read(300)
        has_hdr = probe.startswith(b'MIUI BACKUP')
        expect = os.path.getsize(args.src)
        print(f'\n源文件: {args.src}  ({human(expect)})')
        print(f'格式  : {"完整 .bak（含 66 字节 MIUI 头）" if has_hdr else "纯 tar（将补写 MIUI 头）"}')
        if not has_hdr and probe[257:263] != b'ustar\x00':
            print(f'  ⚠ 偏移 257 处 magic = {probe[257:263]!r}，可能不是 GNU tar')
        with open(args.src, 'rb') as f:
            res = surgery(f, args.out, repl, has_hdr, expect)

    if res is None:
        return 1
    ok = validate(args.out)
    print('\n  下一步 S8-b：')
    print(f'    python make_descript.py --bak {args.out} --template <原descript.xml> --out descript.xml')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
