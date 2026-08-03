#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S5 SQLCipher 密钥推导与验证。

自动采集 IMEI / android_id / UIN，按候选矩阵逐个尝试打开数据库，
找到能成功读取 sqlite_master 的组合。

用法:
    # 全自动：从设备取 IMEI/UIN，遍历候选矩阵
    python get_wechat_key.py --db EnMicroMsg_corrupted.db --auto

    # 手动指定
    python get_wechat_key.py --db EnMicroMsg_corrupted.db --imei 8678... --uin 123456789

    # 已知密钥，仅验证
    python get_wechat_key.py --db EnMicroMsg_corrupted.db --key d994c64
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys

MM_EXT = '/sdcard/Android/data/com.tencent.mm/MicroMsg'


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
    hint = 'pip install sqlcipher3-binary' if v < (3, 12) else 'pip install sqlcipher3  或  pip install pysqlcipher3'
    print(f'[错误] 未安装 SQLCipher 绑定。Python {v.major}.{v.minor} 建议: {hint}')
    sys.exit(1)


def sh(adb, serial, cmd, timeout=30):
    base = [adb] + (['-s', serial] if serial else []) + ['shell', cmd]
    try:
        return subprocess.run(base, capture_output=True, text=True,
                              timeout=timeout, errors='replace').stdout.strip()
    except Exception:
        return ''


def exec_out(adb, serial, cmd, timeout=60):
    base = [adb] + (['-s', serial] if serial else []) + ['exec-out', cmd]
    try:
        return subprocess.run(base, capture_output=True, timeout=timeout).stdout
    except Exception:
        return b''


def parse_service_call_imei(raw):
    """解析 adb shell service call iphonesubinfo 1 的 parcel 输出"""
    chars = re.findall(r"'([^']*)'", raw)
    s = ''.join(chars).replace('.', '').replace(' ', '')
    digits = re.sub(r'\D', '', s)
    return digits if 14 <= len(digits) <= 17 else ''


def collect_device_ids(adb, serial):
    ids = {}
    for slot in (1, 3):        # 1=IMEI1, 3=IMEI2(部分ROM)
        raw = sh(adb, serial, f'service call iphonesubinfo {slot}')
        imei = parse_service_call_imei(raw)
        if imei:
            ids[f'imei{slot}'] = imei
    aid = sh(adb, serial, 'settings get secure android_id')
    if aid and aid != 'null':
        ids['android_id'] = aid.strip()
    sn = sh(adb, serial, 'getprop ro.serialno')
    if sn:
        ids['serialno'] = sn.strip()
    return ids


def collect_uins(adb, serial, hashes=None):
    """从外部存储的 prefs 里搜集 UIN 候选"""
    uins = set()
    listing = sh(adb, serial, f'ls {MM_EXT}/ 2>/dev/null')
    hs = hashes or [x for x in listing.split() if re.fullmatch(r'[a-f0-9]{32}', x.strip())]
    for h in hs:
        for fn in ('system_config_prefs.xml', 'CompatibleInfo.cfg'):
            data = exec_out(adb, serial, f'cat "{MM_EXT}/{h}/{fn}" 2>/dev/null')
            if not data:
                continue
            txt = data.decode('utf-8', errors='replace')
            for m in re.finditer(r'(?:default_uin|_auth_uin|uin)"?\s*(?:value=)?"?(-?\d{6,12})', txt):
                uins.add(m.group(1))
    # 需 root 的路径
    data = exec_out(adb, serial, 'su -c "cat /data/data/com.tencent.mm/shared_prefs/auth_info_key_prefs.xml" 2>/dev/null')
    if data:
        txt = data.decode('utf-8', errors='replace')
        for m in re.finditer(r'_auth_uin"\s*value="(-?\d+)"', txt):
            uins.add(m.group(1))
    return sorted(uins)


def uin_variants(uin):
    """UIN 可能以有符号/无符号两种形式参与推导"""
    out = {str(uin)}
    try:
        n = int(uin)
        out.add(str(n))
        out.add(str(n & 0xFFFFFFFF))            # 无符号形式
        if n > 0x7FFFFFFF:
            out.add(str(n - 0x100000000))       # 有符号形式
    except ValueError:
        pass
    return sorted(out)


def build_candidates(ids, uins, createmd5):
    """构造 (描述, 密钥, cipher_compat) 候选列表，按成功率排序"""
    cands = []
    devs = []
    for k in ('imei1', 'imei3', 'android_id', 'serialno'):
        if ids.get(k):
            devs.append((k, ids[k]))
    devs.append(('default', '1234567890ABCDEF'))   # 无 IMEI 时微信的默认值

    for uin in uins:
        for uv in uin_variants(uin):
            for dname, dval in devs:
                k = hashlib.md5((dval + uv).encode()).hexdigest()[:7]
                cands.append((f'MD5({dname}+uin{uv})[:7]', k, 1))
    for uin in uins:
        for uv in uin_variants(uin):
            for dname, dval in devs:
                k = hashlib.md5((uv + dval).encode()).hexdigest()[:7]
                cands.append((f'MD5(uin{uv}+{dname})[:7]', k, 1))

    if createmd5:
        cands.append(('ini.createmd5 全值', createmd5, 1))
        cands.append(('ini.createmd5[:7]', createmd5[:7], 1))
        cands.append((f"raw x'{createmd5}'", f"x'{createmd5}'", 1))
        cands.append((f"raw x'{createmd5}' compat3", f"x'{createmd5}'", 3))

    # 其他 cipher_compatibility
    extra = []
    for desc, k, _ in list(cands)[:20]:
        for c in (3, 4):
            extra.append((f'{desc} compat{c}', k, c))
    cands += extra

    # 去重保序
    seen, uniq = set(), []
    for d, k, c in cands:
        if (k, c) in seen:
            continue
        seen.add((k, c))
        uniq.append((d, k, c))
    return uniq


def try_key(sq, db, key, compat):
    """返回 (是否成功, 消息数或错误)"""
    conn = None
    try:
        conn = sq.connect(db)
        cur = conn.cursor()
        if key.startswith("x'"):
            cur.execute(f'PRAGMA key = "{key}"')
        else:
            cur.execute(f"PRAGMA key = '{key}'")
        cur.execute(f'PRAGMA cipher_compatibility = {compat}')
        cur.execute('SELECT count(*) FROM sqlite_master')
        n_obj = cur.fetchone()[0]
        n_msg = None
        try:
            cur.execute('SELECT count(*) FROM message')
            n_msg = cur.fetchone()[0]
        except Exception:
            pass
        return True, (n_obj, n_msg)
    except Exception as e:
        return False, str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    ap.add_argument('--auto', action='store_true', help='从设备自动采集 IMEI/UIN')
    ap.add_argument('--imei', action='append', default=[])
    ap.add_argument('--uin', action='append', default=[])
    ap.add_argument('--key', default=None, help='已知密钥，仅验证')
    ap.add_argument('--compat', type=int, default=1)
    ap.add_argument('--ini', default=None, help='EnMicroMsg.db.ini 路径（取 createmd5）')
    args = ap.parse_args()

    sq = load_sqlcipher()

    print('=' * 62)
    print('S5 · SQLCipher 密钥推导与验证')
    print('=' * 62)

    if not os.path.exists(args.db):
        print(f'[错误] 数据库不存在: {args.db}')
        return 1
    print(f'\n数据库: {args.db}  ({os.path.getsize(args.db)/1024**3:.2f} GB)')
    with open(args.db, 'rb') as f:
        print(f'salt   : {f.read(16).hex()}')

    # 已知密钥，直接验证
    if args.key:
        ok, info = try_key(sq, args.db, args.key, args.compat)
        if ok:
            n_obj, n_msg = info
            print(f'\n✓ 密钥有效: {args.key}  (cipher_compatibility={args.compat})')
            print(f'  sqlite_master 对象数: {n_obj}   message 行数: {n_msg}')
            return 0
        print(f'\n✗ 密钥无效: {info}')
        print('  提示：「file is not a database」= 密钥错，不是文件坏')
        return 1

    # createmd5
    createmd5 = None
    ini = args.ini
    if not ini:
        guess = args.db + '.ini'
        alt = args.db.replace('.db', '.db.ini')
        ini = guess if os.path.exists(guess) else (alt if os.path.exists(alt) else None)
    if ini and os.path.exists(ini):
        m = re.search(r'createmd5=([0-9a-fA-F]{32})',
                      open(ini, 'r', encoding='utf-8', errors='replace').read())
        if m:
            createmd5 = m.group(1)
            print(f'createmd5: {createmd5}  (来自 {os.path.basename(ini)})')

    ids = {}
    uins = list(args.uin)
    for i, v in enumerate(args.imei):
        ids[f'imei{1 if i == 0 else 3}'] = v

    if args.auto:
        print('\n[采集设备标识]')
        ids.update(collect_device_ids(args.adb, args.serial))
        for k, v in ids.items():
            print(f'  {k:<12}: {v}')
        print('\n[采集 UIN]')
        found = collect_uins(args.adb, args.serial)
        uins = list(dict.fromkeys(uins + found))
        print(f'  候选 UIN: {uins if uins else "未找到（见 05-troubleshooting.md Q5）"}')

    if not uins and not createmd5:
        print('\n[错误] 既无 UIN 也无 createmd5，无法构造候选。')
        print('  请手动提供：--uin <数字>  或  --imei <IMEI> --uin <数字>')
        return 1

    cands = build_candidates(ids, uins, createmd5)
    print(f'\n[开始遍历候选矩阵] 共 {len(cands)} 个组合')
    for i, (desc, key, compat) in enumerate(cands, 1):
        ok, info = try_key(sq, args.db, key, compat)
        status = 'OK ' if ok else '   '
        if ok or i <= 10 or i % 20 == 0:
            print(f'  [{i:>3}/{len(cands)}] {status} {desc:<38} key={key} compat={compat}')
        if ok:
            n_obj, n_msg = info
            print('\n' + '=' * 62)
            print('✓ 找到有效密钥')
            print('=' * 62)
            print(f'  KEY                  = {key}')
            print(f'  cipher_compatibility = {compat}')
            print(f'  推导方式             = {desc}')
            print(f'  sqlite_master 对象数 = {n_obj}')
            print(f'  message 行数         = {n_msg if n_msg is not None else "读取失败（表可能损坏，属正常）"}')
            print(f'\n  下一步 S6：')
            print(f'    python recover_database.py {args.db} EnMicroMsg_recovered.db {key} {compat}')
            return 0

    print('\n✗ 候选矩阵全部失败。请参考 references/03-key-and-repair.md：')
    print('  1) 确认 UIN 正确（有符号/无符号两种形式都试过了）')
    print('  2) 双卡机两个 IMEI 都要试')
    print('  3) 尝试从 MIUI 备份包提取 auth_info_key_prefs.xml')
    print('  4) 极少数版本未加密，可直接用普通 sqlite3 打开试试')
    return 1


if __name__ == '__main__':
    sys.exit(main())
