#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S10 双端清理（P6 列清单 + 双确认 + P5 原始数据最后删）。

职责：
  1) 在 --dry-run 下列出所有将要删除的「电脑端 + 手机端」文件/目录（路径 + 大小），
     供用户逐项核对；
  2) 在 --confirm 下执行清理（电脑端走回收站，超大文件直接删；手机端用 adb rm 显式路径）；
  3) 手机上的 corrupted/ 原始数据源 **必须** 额外加 --delete-corrupted 才会删，
     这是「单独确认」的硬性闸门——它是唯一的重做资本。

安全红线（见 references/06-cleanup-checklist.md 第六节）：
  - 绝不对 Desktop / Downloads / Documents / 用户主目录 递归删除
  - 绝不用 rm -rf *.bak 这类通配批删；脚本内部用 glob 仅用于「发现」候选，
    真正删除时逐条打印完整显式路径，且必须 --confirm
  - 绝不在用户确认「恢复成功」之前调用本脚本做破坏性删除
  - 绝不删除微信当前正在使用的 EnMicroMsg.db 本体

用法:
    # 1) 先看清单（什么都不删）
    python cleanup.py --workdir <workdir> --hash <32位hash> --dry-run

    # 2) 用户确认清单无误后正式清理（不含 corrupted/）
    python cleanup.py --workdir <workdir> --hash <hash> \
        --bak-orig 20260801_140743 --bak-new 20260801_153000 \
        --archive "D:\\聊天数据恢复" --confirm

    # 3) 用户最终确认微信稳定后，再单独删手机 corrupted/（单独闸门）
    python cleanup.py --workdir <workdir> --hash <hash> --confirm --delete-corrupted

说明：
  --bak-orig / --bak-new 省略时，对应手机备份目录会被跳过（不误删）。
  --archive 仅用于报告归档位置，不会被清理。
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

MM_EXT = '/sdcard/Android/data/com.tencent.mm/MicroMsg'
HUGE_BYTES = 30 * 1024 ** 3  # ≥30GB 直接删除，避免回收站占满磁盘

# ---------- 工具 ----------
def human(n):
    return f'{n / 1024 ** 3:.2f} GB' if n >= 1024 ** 3 else f'{n / 1024 ** 2:.1f} MB'


def sh(adb, serial, cmd, timeout=120):
    base = [adb] + (['-s', serial] if serial else []) + ['shell', cmd]
    try:
        return subprocess.run(base, capture_output=True, text=True,
                              timeout=timeout, errors='replace').stdout.strip()
    except Exception:
        return ''


def human_remote(adb, serial, path):
    out = sh(adb, serial, f'du -sb "{path}" 2>/dev/null')
    m = re.match(r'(\d+)', out)
    return int(m.group(1)) if m else 0


def local_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ---------- 电脑端候选（仅在工作目录内，绝不触碰个人目录） ----------
# 必须保留的文件（白名单，绝不进入删除列表）
KEEP_PC = {
    'wechat_final.bak',
    'EnMicroMsg_recovered.db',
    'EnMicroMsg_recovered.db.sm',
    'EnMicroMsg_corrupted.db.sm',
    'EnMicroMsg_corrupted.db.li',
    'EnMicroMsg_corrupted.db.ini',
    'descript.xml',                 # 最终版保留
}

# 明确要删的中间产物（文件名模式，仅作为「发现」条件；真正删除逐条列出路径）
PC_DEL_PATTERNS = [
    'wechat_new.bak', 'wechat_v1.bak', 'wechat_v2.bak',
    'wechat_orig.bak', 'wechat.tar',
    'EnMicroMsg_corrupted.db',       # 3GB 级；空间允许建议保留，故列入但 dry-run 会标注
    '*.log', 'restore_logcat*.txt',
    '*.png',
    'try_keys*.py', 'count_*.py', 'extract_*.py',
    'part1.bin', 'zip_tail.bin', 'miui_header.bin',
    'descript_new.xml', 'descript_orig.xml',
]

PC_DEL_DIRS = ['extracted', 'temp', 'work_tmp']


def pc_candidates(workdir):
    """返回 [(path, size, note)] —— 仅 workdir 内的中间产物。"""
    out = []
    for name in PC_DEL_PATTERNS:
        if name in KEEP_PC:
            continue
        if '*' in name:
            for p in glob.glob(os.path.join(workdir, name)):
                if os.path.isfile(p):
                    out.append((p, local_size(p), ''))
        else:
            p = os.path.join(workdir, name)
            if os.path.isfile(p):
                note = '（空间允许可保留；仅删中间版本）' if name == 'EnMicroMsg_corrupted.db' else ''
                out.append((p, local_size(p), note))
    for d in PC_DEL_DIRS:
        dp = os.path.join(workdir, d)
        if os.path.isdir(dp):
            sz = 0
            for root, _, files in os.walk(dp):
                for f in files:
                    sz += local_size(os.path.join(root, f))
            out.append((dp, sz, '（目录）'))
    # 去重
    seen, uniq = set(), []
    for p, s, n in out:
        if p not in seen:
            seen.add(p)
            uniq.append((p, s, n))
    return uniq


# ---------- 手机端候选（显式路径，绝不通配批删） ----------
def phone_candidates(adb, serial, h, bak_orig, bak_new):
    out = []  # (remote_path, note, is_corrupted)

    def add_if_exists(path, note, corrupted=False):
        if human_remote(adb, serial, path):
            out.append((path, note, corrupted))

    # 1) 临时工作目录（体积最大，优先删）
    for d in ['/sdcard/temp_bak', '/sdcard/temp_full', '/sdcard/temp_extract']:
        add_if_exists(d, '（临时工作目录）')

    # 2) 手术用的原始备份（已归档）
    if bak_orig:
        add_if_exists(f'/sdcard/MIUI/backup/AllBackup/{bak_orig}.zip.hidden',
                      '（手术用原始备份·隐藏态）')
        add_if_exists(f'/sdcard/MIUI/backup/AllBackup/{bak_orig}',
                      '（手术用原始备份目录）')

    # 3) 回传用的备份目录（已归档）
    if bak_new:
        add_if_exists(f'/sdcard/MIUI/backup/AllBackup/{bak_new}',
                      '（回传用备份目录）')

    # 5) WAL/SHM 残留（微信会自建，删掉无害）—— 用显式 hash 或单级通配
    wal_base = f'{MM_EXT}/{h}' if h else f'{MM_EXT}/*'
    add_if_exists(f'{wal_base}/EnMicroMsg.db-wal', '（WAL 残留）')
    add_if_exists(f'{wal_base}/EnMicroMsg.db-shm', '（SHM 残留）')

    # 6) corrupted/ 原始数据源（单独闸门，is_corrupted=True）
    corr_base = f'{MM_EXT}/{h}' if h else f'{MM_EXT}/*'
    add_if_exists(f'{corr_base}/corrupted', '（⚠ 原始数据源·需单独闸门）', corrupted=True)

    return out


# ---------- 删除执行 ----------
def trash_or_remove(path, dry):
    if dry:
        return
    sz = local_size(path)
    if sz >= HUGE_BYTES:
        # 超大文件直接删，避免回收站占满
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return
    # 优先移入回收站（Windows）
    try:
        ps = (
            'powershell -NoProfile -Command '
            '$sh=New-Object -ComObject Shell.Application;'
            '$ns=$sh.NameSpace(0);'
            f'$item=$ns.ParseName(\\\"{path}\\\");'
            'if($item){{$item.InvokeVerb(\"delete\")}}'
        )
        r = subprocess.run(ps, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and not os.path.exists(path):
            return
    except Exception:
        pass
    # 回收站失败则直接删
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def rm_phone(adb, serial, path, dry, is_dir=True):
    if dry:
        return
    flag = '-rf' if is_dir else '-f'
    sh(adb, serial, f'rm {flag} "{path}"')


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    ap.add_argument('--workdir', required=True, help='本地工作目录（仅清理此目录内中间产物）')
    ap.add_argument('--hash', default=None, help='微信 32 位 hash 目录名（用于手机路径）')
    ap.add_argument('--bak-orig', default=None, help='手术用原始备份日期目录名（如 20260801_140743）')
    ap.add_argument('--bak-new', default=None, help='回传用备份日期目录名')
    ap.add_argument('--archive', default=None, help='归档目录（仅用于报告，不清理）')
    ap.add_argument('--restore-hidden', default=None, help='可选：把该 .zip.hidden 改回 .zip')
    ap.add_argument('--dry-run', action='store_true', help='只列清单，不删除')
    ap.add_argument('--confirm', action='store_true', help='确认后执行清理（不含 corrupted/）')
    ap.add_argument('--delete-corrupted', action='store_true',
                    help='仅当同时带 --confirm 时，才删除手机 corrupted/（单独闸门）')
    args = ap.parse_args()

    if args.delete_corrupted and not args.confirm:
        print('✗ --delete-corrupted 必须与 --confirm 同时使用，且应在用户最终确认微信稳定后单独执行。')
        return 2
    if not args.dry_run and not args.confirm:
        print('✗ 必须指定 --dry-run（先看清单）或 --confirm（确认后执行）。')
        return 2

    print('=' * 66)
    print('S10 · 双端清理（P6 列清单 + 双确认 + P5 原始数据最后删）')
    print('=' * 66)
    if args.confirm:
        print('\n⚠️ 此操作非常危险，可能导致不可逆的数据丢失！')
        print('   仅在用户已明确确认「聊天记录已恢复且稳定」、且回滚点已归档后执行。')
        if not args.delete_corrupted:
            print('   本次不含 corrupted/（需用 --delete-corrupted 单独触发）。')

    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        print(f'✗ 工作目录不存在：{workdir}')
        return 1

    # ---- 收集候选 ----
    pc = pc_candidates(workdir)
    ph = phone_candidates(args.adb, args.serial, args.hash, args.bak_orig, args.bak_new)

    pc_total = sum(s for _, s, _ in pc)
    ph_total = sum(human_remote(args.adb, args.serial, p) for p, _, _ in ph if not args.dry_run) \
        if args.dry_run else 0
    # dry-run 下手机大小也探测
    if args.dry_run:
        ph_with_size = [(p, human_remote(args.adb, args.serial, p), n, c) for p, n, c in ph]
    else:
        ph_with_size = [(p, 0, n, c) for p, n, c in ph]

    print(f'\n[电脑端] 工作目录：{workdir}')
    print(f'  待删 {len(pc)} 项，合计约 {human(pc_total)}')
    for p, s, n in pc:
        print(f'    - {p}  ({human(s)})  {n}')

    print(f'\n[手机端]')
    ph_normal = [x for x in ph_with_size if not x[3]]
    ph_corr = [x for x in ph_with_size if x[3]]
    print(f'  常规待删 {len(ph_normal)} 项：')
    for p, s, n, _ in ph_normal:
        print(f'    - {p}  ({human(s) if args.dry_run else "?"})  {n}')
    print(f'  corrupted/ 单独闸门 {len(ph_corr)} 项（需 --delete-corrupted）：')
    for p, s, n, _ in ph_corr:
        print(f'    - {p}  ({human(s) if args.dry_run else "?"})  {n}')

    if args.archive:
        print(f'\n[归档] 回滚点已归档至：{args.archive}（此目录不会被清理）')

    # ---- 执行 ----
    if args.dry_run:
        print('\n' + '=' * 66)
        print('✓ 以上为待删清单。请逐项核对无误后，用 --confirm 执行。')
        print('=' * 66)
        return 0

    # --confirm 执行
    print('\n[执行] 电脑端清理…')
    for p, s, n in pc:
        print(f'  删 {p}  ({human(s)}) {n}')
        try:
            trash_or_remove(p, False)
            print('     ✓')
        except Exception as e:
            print(f'     ✗ 失败：{e}')

    print('\n[执行] 手机端常规清理…')
    for p, n, c in ph:
        if c:
            continue  # corrupted 单独闸门
        print(f'  删 {p}  {n}')
        rm_phone(args.adb, args.serial, p, False, is_dir=True)

    # 可选：恢复被改名的隐藏备份
    if args.restore_hidden:
        print(f'\n[执行] 恢复隐藏名：{args.restore_hidden} -> .zip')
        sh(args.adb, args.serial,
            f'mv "{args.restore_hidden}.zip.hidden" "{args.restore_hidden}.zip"')

    # corrupted 单独闸门
    if args.delete_corrupted:
        print('\n[执行] 手机 corrupted/ 删除（单独闸门已开启）…')
        for p, n, c in ph:
            if not c:
                continue
            print(f'  ⚠ 删除原始数据源 {p}  {n}')
            rm_phone(args.adb, args.serial, p, False, is_dir=True)
    else:
        print('\n  注：corrupted/ 未删除（保留为最终数据源）。')
        print('      待用户最终确认微信稳定后，再用 --delete-corrupted 单独清除。')

    print('\n' + '=' * 66)
    print('✓ 清理完成。')
    print('  核对空间：')
    print(f'    电脑：剩余 {shutil.disk_usage(workdir).free / 1024**3:.1f} GB')
    if args.serial or True:
        df = sh(args.adb, args.serial, 'df -h /sdcard 2>/dev/null | tail -1')
        if df:
            print(f'    手机：{df}')
    print('=' * 66)
    return 0


if __name__ == '__main__':
    sys.exit(main())
