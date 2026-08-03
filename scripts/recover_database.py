#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S6 数据库修复：从损坏的加密库分批读出可用数据，写入全新的干净加密库。

策略：按主键分批（500）读取，遇坏页自动降批（50 → 逐条），跳过读不出的页。
新库使用同一密钥加密，微信才能打开。

用法:
    python recover_database.py <corrupted_db> <recovered_db> <key> [cipher_compat]
示例:
    python recover_database.py EnMicroMsg_corrupted.db EnMicroMsg_recovered.db d994c64 1
"""
import os
import sys
import time

# FTS5 全文索引不恢复——微信会自行重建，强行复制反而可能触发完整性检查失败
SKIP_TABLE_PATTERNS = ('fts5', 'FTS5', '_content', '_segdir', '_segments', '_docsize', '_stat')

BATCH = 500
SUB_BATCH = 50


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
    v = sys.version_info
    hint = 'pip install sqlcipher3-binary' if v < (3, 12) else 'pip install sqlcipher3 / pysqlcipher3'
    print(f'[错误] 未安装 SQLCipher 绑定。Python {v.major}.{v.minor} 建议: {hint}')
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


def should_skip(name):
    return any(p in name for p in SKIP_TABLE_PATTERNS)


def recover(src_path, dst_path, key, compat=1):
    sq = load_sqlcipher()

    print('=' * 62)
    print('S6 · 数据库修复')
    print('=' * 62)
    print(f'源库  : {src_path}  ({os.path.getsize(src_path)/1024**3:.2f} GB)')
    print(f'目标  : {dst_path}')
    print(f'密钥  : {key}   cipher_compatibility = {compat}')

    if os.path.exists(dst_path):
        os.remove(dst_path)
    for ext in ('-wal', '-shm'):
        if os.path.exists(dst_path + ext):
            os.remove(dst_path + ext)

    conn_src, cur_src = open_db(sq, src_path, key, compat)
    conn_dst, cur_dst = open_db(sq, dst_path, key, compat)

    # 目标库写入优化
    for pragma in ('PRAGMA journal_mode = OFF', 'PRAGMA synchronous = OFF',
                   'PRAGMA temp_store = MEMORY', 'PRAGMA cache_size = -200000'):
        try:
            cur_dst.execute(pragma)
        except Exception:
            pass

    cur_src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
    tables = [(n, s) for n, s in cur_src.fetchall() if not should_skip(n)]
    print(f'\n发现 {len(tables)} 张待处理表（已排除 FTS5 相关）\n')

    start = time.time()
    total_ok = total_bad = 0
    summary = []

    for ti, (tname, schema) in enumerate(tables, 1):
        try:
            cur_dst.execute(schema)
            conn_dst.commit()
        except Exception as e:
            print(f'[{ti}/{len(tables)}] {tname}: 建表失败 {e}')
            continue

        cur_src.execute(f'PRAGMA table_info("{tname}")')
        cols = cur_src.fetchall()
        if not cols:
            continue
        col_names = [c[1] for c in cols]
        col_list = ','.join(f'"{c}"' for c in col_names)
        ph = ','.join('?' * len(col_names))
        ins = f'INSERT OR IGNORE INTO "{tname}" ({col_list}) VALUES ({ph})'

        pk = next((c[1] for c in cols if c[5] and c[5] > 0), None)
        rng = None
        if pk:
            try:
                cur_src.execute(f'SELECT min("{pk}"), max("{pk}") FROM "{tname}"')
                lo, hi = cur_src.fetchone()
                if lo is not None:
                    rng = (int(lo), int(hi))
            except Exception:
                rng = None

        t_ok = t_bad = 0

        if rng:
            lo, hi = rng
            cur = lo
            last_report = time.time()
            while cur <= hi:
                end = min(cur + BATCH - 1, hi)
                try:
                    cur_src.execute(
                        f'SELECT {col_list} FROM "{tname}" WHERE "{pk}" >= ? AND "{pk}" <= ?',
                        (cur, end))
                    rows = cur_src.fetchall()
                    if rows:
                        cur_dst.executemany(ins, rows)
                        conn_dst.commit()
                        t_ok += len(rows)
                except Exception:
                    # 降批：500 -> 50
                    for s in range(cur, end + 1, SUB_BATCH):
                        e2 = min(s + SUB_BATCH - 1, end)
                        try:
                            cur_src.execute(
                                f'SELECT {col_list} FROM "{tname}" WHERE "{pk}" >= ? AND "{pk}" <= ?',
                                (s, e2))
                            rows = cur_src.fetchall()
                            if rows:
                                cur_dst.executemany(ins, rows)
                                t_ok += len(rows)
                        except Exception:
                            # 再降批：逐条
                            for one in range(s, e2 + 1):
                                try:
                                    cur_src.execute(
                                        f'SELECT {col_list} FROM "{tname}" WHERE "{pk}" = ?', (one,))
                                    rows = cur_src.fetchall()
                                    if rows:
                                        cur_dst.executemany(ins, rows)
                                        t_ok += len(rows)
                                except Exception:
                                    t_bad += 1
                    conn_dst.commit()
                cur = end + 1
                # P7 进度上报
                if time.time() - last_report > 20:
                    pct = (cur - lo) / max(hi - lo, 1) * 100
                    print(f'    {tname}: {pct:5.1f}%  已恢复 {t_ok} 行  丢失 {t_bad} 行')
                    last_report = time.time()
        else:
            # 无主键：整表读，失败则放弃
            try:
                cur_src.execute(f'SELECT {col_list} FROM "{tname}"')
                rows = cur_src.fetchall()
                if rows:
                    cur_dst.executemany(ins, rows)
                    conn_dst.commit()
                    t_ok = len(rows)
            except Exception as e:
                print(f'    {tname}: 整表读取失败 {str(e)[:60]}')

        total_ok += t_ok
        total_bad += t_bad
        summary.append((tname, t_ok, t_bad))
        mark = '  ⚠' if t_bad else ''
        print(f'[{ti:>3}/{len(tables)}] {tname:<28} 恢复 {t_ok:>9} 行  丢失 {t_bad:>6} 行{mark}')

    # 索引
    print('\n重建索引...')
    cur_src.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    idx_ok = idx_fail = 0
    for iname, isql in cur_src.fetchall():
        if should_skip(iname):
            continue
        try:
            cur_dst.execute(isql)
            idx_ok += 1
        except Exception:
            idx_fail += 1
    conn_dst.commit()
    print(f'  成功 {idx_ok}，跳过 {idx_fail}')

    # 完整性检查
    print('\n完整性检查 (PRAGMA integrity_check)...')
    cur_dst.execute('PRAGMA integrity_check')
    res = cur_dst.fetchall()
    passed = (len(res) == 1 and res[0][0] == 'ok')
    for r in res[:10]:
        print(f'  {r[0]}')

    conn_src.close()
    conn_dst.close()

    el = time.time() - start
    print('\n' + '=' * 62)
    print('修复完成')
    print('=' * 62)
    print(f'  总恢复行数 : {total_ok:,}')
    print(f'  总丢失行数 : {total_bad:,}' + (f'  ({total_bad/max(total_ok+total_bad,1)*100:.2f}%)' if total_bad else ''))
    print(f'  输出文件   : {dst_path}  ({os.path.getsize(dst_path)/1024**3:.2f} GB)')
    print(f'  耗时       : {el/60:.1f} 分钟')
    print(f'  完整性     : {"✓ ok" if passed else "✗ 未通过，见上方输出"}')

    top = sorted(summary, key=lambda x: -x[1])[:8]
    print('\n  主要表恢复情况:')
    for n, ok, bad in top:
        print(f'    {n:<28} {ok:>10,} 行' + (f'  (丢失 {bad})' if bad else ''))

    print('\n  下一步：')
    print(f'    1) python update_sm_salt.py {dst_path} <corrupted.db.sm> <recovered.db.sm>')
    print(f'    2) python verify_gate.py --db {dst_path} --key <KEY> --src <corrupted.db>')
    return 0 if passed else 1


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    sys.exit(recover(sys.argv[1], sys.argv[2], sys.argv[3],
                     int(sys.argv[4]) if len(sys.argv) > 4 else 1))
