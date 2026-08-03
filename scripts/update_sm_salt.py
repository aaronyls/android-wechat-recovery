#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S6-b 同步 .sm 文件的 salt。

新建的恢复库有全新的随机 salt（文件前 16 字节），必须写入 .sm 文件的
偏移 12:28，否则微信打开时校验失败。这一步极易遗漏。

.sm 结构:  [0:12] 头部 00 64 42 6d 53 74 01 00 4b 02 00 00 ("dBmSt")
           [12:28] 数据库 salt 副本
           [28:]  加密数据

用法:
    python update_sm_salt.py <recovered_db> <src_sm> <out_sm>
示例:
    python update_sm_salt.py EnMicroMsg_recovered.db EnMicroMsg_corrupted.db.sm EnMicroMsg_recovered.db.sm
"""
import os
import sys

EXPECTED_MAGIC = bytes.fromhex('0064426d5374')   # \x00 dBmSt


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1

    db_path, src_sm, out_sm = sys.argv[1], sys.argv[2], sys.argv[3]

    for p in (db_path, src_sm):
        if not os.path.exists(p):
            print(f'[错误] 文件不存在: {p}')
            return 1

    print('=' * 62)
    print('S6-b · 同步 .sm salt')
    print('=' * 62)

    with open(db_path, 'rb') as f:
        new_salt = f.read(16)
    print(f'\n恢复库      : {db_path}')
    print(f'  新 salt   : {new_salt.hex()}')

    with open(src_sm, 'rb') as f:
        sm = bytearray(f.read())
    print(f'\n源 .sm      : {src_sm}  ({len(sm)} bytes)')
    print(f'  头部(0:12): {bytes(sm[:12]).hex()}')
    print(f'  旧 salt   : {bytes(sm[12:28]).hex()}')

    if len(sm) < 28:
        print('[错误] .sm 文件过短（< 28 字节），格式异常')
        return 1
    if bytes(sm[:6]) != EXPECTED_MAGIC:
        print(f'[警告] .sm 头部 magic 与预期不符（预期 {EXPECTED_MAGIC.hex()}）')
        print('       仍将继续，但请确认该文件确实是微信 .sm 文件')

    if bytes(sm[12:28]) == new_salt:
        print('\n  salt 已一致，无需修改。')
        if os.path.abspath(src_sm) != os.path.abspath(out_sm):
            with open(out_sm, 'wb') as f:
                f.write(bytes(sm))
            print(f'  已复制到: {out_sm}')
        return 0

    sm[12:28] = new_salt
    with open(out_sm, 'wb') as f:
        f.write(bytes(sm))

    # 回读验证
    with open(out_sm, 'rb') as f:
        chk = f.read(28)
    ok = chk[12:28] == new_salt and len(open(out_sm, 'rb').read()) == len(sm)

    print(f'\n输出 .sm    : {out_sm}')
    print(f'  新 salt   : {chk[12:28].hex()}')
    print(f'  校验      : {"✓ 通过" if ok else "✗ 失败"}')
    print(f'\n  打包时请使用 {os.path.basename(out_sm)} 替换 tar 中的 EnMicroMsg.db.sm')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
