#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S7 验证门禁（硬性拦截）—— 本流程最重要的一步。

回传前必须全部通过，否则退回 S6 重修。跳过这一关的代价是一整轮返工：
打包 25min + 回传 15min + 手机恢复 20min，且微信会再次把库判损清空。

检查项:
  1. 恢复库能用原密钥打开
  2. PRAGMA integrity_check 返回单行 ok
  3. message 表行数 >= 源库可读行数的 95%
  4. rcontact 表非空
  5. .sm 文件 salt == 恢复库前 16 字节

用法:
    python verify_gate.py --db EnMicroMsg_recovered.db --key d994c64 \
        [--compat 1] [--src EnMicroMsg_corrupted.db] [--sm EnMicroMsg_recovered.db.sm]
"""
import argparse
import os
import sys


def load_sqlcipher():
    try:
        import sqlcipher3
        return sqlcipher3
    except ImportError:
        pass
    try:
        from pysqlcipher3 import dbapi2
        return dbapi2
    except ImportError:
        pass
    print('[错误] 未安装 SQLCipher 绑定')
    sys.exit(1)


def open_db(sq, path, key, compat):
    conn = sq.connect(path)
    cur = conn.cursor()
    if key.startswith("x'"):
        cur.execute(f'PRAGMA key = "{key}"')
    else:
        cur.execute(f"PRAGMA key = '{key}'")
    cur.execute(f'PRAGMA cipher_compatibility = {compat}')
    return conn, cur


def count(cur, table):
    try:
        cur.execute(f'SELECT count(*) FROM "{table}"')
        return cur.fetchone()[0]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True, help='恢复后的数据库')
    ap.add_argument('--key', required=True)
    ap.add_argument('--compat', type=int, default=1)
    ap.add_argument('--src', default=None, help='原损坏库，用于行数比对')
    ap.add_argument('--sm', default=None, help='更新 salt 后的 .sm 文件')
    ap.add_argument('--threshold', type=float, default=0.95, help='行数保留率下限')
    args = ap.parse_args()

    sq = load_sqlcipher()
    results = []

    print('=' * 62)
    print('S7 · 验证门禁')
    print('=' * 62)

    if not os.path.exists(args.db):
        print(f'[错误] 数据库不存在: {args.db}')
        return 1
    print(f'\n目标库: {args.db}  ({os.path.getsize(args.db)/1024**3:.2f} GB)')

    # 检查 1: 能否打开
    print('\n[1/5] 用原密钥打开')
    try:
        conn, cur = open_db(sq, args.db, args.key, args.compat)
        cur.execute('SELECT count(*) FROM sqlite_master')
        n_obj = cur.fetchone()[0]
        print(f'      ✓ 打开成功，sqlite_master 对象数 {n_obj}')
        results.append(('用原密钥打开', True))
    except Exception as e:
        print(f'      ✗ 打开失败: {e}')
        print('        微信必须能用同一密钥打开这个库，此项不通过绝不能回传')
        results.append(('用原密钥打开', False))
        _report(results)
        return 1

    # 检查 2: integrity_check
    print('\n[2/5] PRAGMA integrity_check')
    try:
        cur.execute('PRAGMA integrity_check')
        res = cur.fetchall()
        ok = (len(res) == 1 and res[0][0] == 'ok')
        if ok:
            print('      ✓ ok')
        else:
            print(f'      ✗ 未通过，前 10 条：')
            for r in res[:10]:
                print(f'        {r[0]}')
            print('        通常原因：写入时磁盘满 / 过程中断 / 带入了损坏的索引定义')
        results.append(('integrity_check', ok))
    except Exception as e:
        print(f'      ✗ 执行失败: {e}')
        results.append(('integrity_check', False))

    # 检查 3: message 行数
    print('\n[3/5] message 表行数比对')
    n_new = count(cur, 'message')
    print(f'      恢复库 message 行数: {n_new if n_new is not None else "读取失败"}')
    if args.src and os.path.exists(args.src):
        try:
            conn_s, cur_s = open_db(sq, args.src, args.key, args.compat)
            n_old = count(cur_s, 'message')
            conn_s.close()
            print(f'      源库   message 行数: {n_old if n_old is not None else "读取失败(坏库正常现象)"}')
            if n_old and n_new:
                ratio = n_new / n_old
                ok = ratio >= args.threshold
                print(f'      保留率: {ratio*100:.2f}%  (下限 {args.threshold*100:.0f}%) '
                      f'{"✓" if ok else "✗"}')
                results.append(('message 保留率', ok))
            else:
                ok = bool(n_new and n_new > 0)
                print(f'      源库行数不可读（坏库常见），仅校验恢复库非空: {"✓" if ok else "✗"}')
                results.append(('message 非空', ok))
        except Exception as e:
            ok = bool(n_new and n_new > 0)
            print(f'      源库打开失败({str(e)[:40]})，仅校验恢复库非空: {"✓" if ok else "✗"}')
            results.append(('message 非空', ok))
    else:
        ok = bool(n_new and n_new > 0)
        print(f'      未提供源库，仅校验非空: {"✓" if ok else "✗"}')
        results.append(('message 非空', ok))

    # 检查 4: rcontact
    print('\n[4/5] rcontact 表（联系人）')
    n_c = count(cur, 'rcontact')
    ok = bool(n_c and n_c > 0)
    print(f'      行数: {n_c}  {"✓" if ok else "✗ 联系人为空，聊天记录将无法正确显示"}')
    results.append(('rcontact 非空', ok))

    # 附加信息
    for t in ('conversation', 'chatroom', 'userinfo'):
        n = count(cur, t)
        if n is not None:
            print(f'      {t:<14}: {n}')
    conn.close()

    # 检查 5: salt 一致性
    print('\n[5/5] .sm salt 一致性')
    sm_path = args.sm
    if not sm_path:
        guess = args.db + '.sm'
        sm_path = guess if os.path.exists(guess) else None
    if sm_path and os.path.exists(sm_path):
        with open(args.db, 'rb') as f:
            db_salt = f.read(16)
        with open(sm_path, 'rb') as f:
            sm = f.read(28)
        ok = sm[12:28] == db_salt
        print(f'      数据库 salt : {db_salt.hex()}')
        print(f'      .sm    salt : {sm[12:28].hex()}')
        print(f'      {"✓ 一致" if ok else "✗ 不一致 —— 运行 update_sm_salt.py 同步"}')
        results.append(('.sm salt 一致', ok))
    else:
        print('      ! 未找到 .sm 文件，跳过（打包前务必确认已同步 salt）')
        results.append(('.sm salt 一致', False))

    return _report(results)


def _report(results):
    print('\n' + '=' * 62)
    print('门禁结论')
    print('=' * 62)
    for name, ok in results:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')
    passed = all(ok for _, ok in results)
    print()
    if passed:
        print('  ✓ 全部通过 —— 允许进入 S8 打包回传')
    else:
        print('  ✗ 存在未通过项 —— 禁止回传，请退回 S6 重新修复')
        print('    【P2 铁律】绝不把未验证的数据推回手机。微信会再次判损并清空。')
    print('=' * 62)
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
