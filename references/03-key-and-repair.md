# SQLCipher 密钥获取与数据库修复原理

---

## 一、微信数据库加密概述

`EnMicroMsg.db` 使用 **SQLCipher** 加密：

```
[16 字节明文 salt][加密页 1][加密页 2] ...
```

- 前 16 字节是**明文 salt**（每次新建库都会随机生成，这点很重要）
- 密钥为 7 字符十六进制字符串，由设备标识与账号推导
- 微信使用 **SQLCipher v1 兼容模式**：`PRAGMA cipher_compatibility = 1`
  （对应 `cipher_page_size=1024`、`kdf_iter=4000`、`HMAC-SHA1`）

**辅助文件**：

| 文件 | 说明 |
|------|------|
| `EnMicroMsg.db.sm` | 安全文件。头部 12 字节 `00 64 42 6d 53 74 01 00 4b 02 00 00`（含 `dBmSt` magic），**偏移 12–28 存放数据库 salt 的副本** |
| `EnMicroMsg.db.li` | 索引文件，随库一起搬运即可 |
| `EnMicroMsg.db.ini` | 文本配置，含 `createmd5=<32位十六进制>` |

> ⚠️ **salt 一致性**：新建的恢复库有**新的随机 salt**，必须同步写入 `.sm` 偏移 12:28，
> 否则微信打开时校验不过。这是最容易遗漏的一步。

---

## 二、密钥推导

### 标准公式
```python
key = hashlib.md5((imei + uin).encode()).hexdigest()[:7]
```

- `imei`：设备 IMEI（15 位数字）
- `uin`：微信账号的 UIN（有符号 32 位整数的十进制字符串，可能为负数）

### UIN 获取途径（按易用性排序）

**1. 免 root：外部存储的 system_config_prefs.xml**
```bash
adb exec-out "cat /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/system_config_prefs.xml"
```
搜索 `default_uin`。

**2. 免 root：从 MIUI 备份包里提取**
备份 tar 中含 `apps/com.tencent.mm/a/shared_prefs/auth_info_key_prefs.xml`，
流式扫描 tar 找到该条目提取即可（本技能 `get_wechat_key.py --from-bak` 支持）。

**3. 需 root**
```bash
adb shell "su -c 'cat /data/data/com.tencent.mm/shared_prefs/auth_info_key_prefs.xml'"
```
找 `_auth_uin`。

**4. 从 CompatibleInfo.cfg**
```
/data/data/com.tencent.mm/MicroMsg/CompatibleInfo.cfg   （Java 序列化的 HashMap）
```

**5. 兜底：暴力枚举**
若能拿到 IMEI，UIN 可在合理范围内枚举验证（每个候选试解密一次），
但空间过大不实用，仅在有部分已知位时可行。

### IMEI 获取
```bash
adb shell service call iphonesubinfo 1        # 解析返回的 parcel
adb shell settings get secure android_id      # 部分场景用 android_id 替代
adb shell getprop ro.serialno                 # 序列号
```

> Android 10+ 普通应用已无法读 IMEI，但 `adb shell service call` 仍可用（需 READ_PRIVILEGED_PHONE_STATE，
> shell 用户通常具备）。双卡机有 IMEI1/IMEI2，**两个都要试**。

---

## 三、密钥候选矩阵（传统公式失败时逐个试）

按成功率排序：

| # | 候选密钥 | cipher_compatibility |
|---|----------|---------------------|
| 1 | `MD5(IMEI1 + UIN)[:7]` | 1 |
| 2 | `MD5(IMEI2 + UIN)[:7]` | 1 |
| 3 | `MD5(android_id + UIN)[:7]` | 1 |
| 4 | `MD5("1234567890ABCDEF" + UIN)[:7]` | 1 | ← 无 IMEI 时微信的默认值 |
| 5 | `MD5(UIN + IMEI)[:7]` | 1 |
| 6 | `.ini` 中 `createmd5` 全值（32 字符） | 1 |
| 7 | `createmd5[:7]` | 1 |
| 8 | `x'<createmd5>'` 作为 16 字节原始密钥 | 1 / 3 |
| 9 | 上述任一 + `cipher_compatibility = 3 或 4` | 3 / 4 |
| 10 | 无密钥（未加密库，少见于旧版/国际版） | — |

**验证方式**（不要用 `integrity_check` 来验证密钥，坏库必然报错）：
```python
cur.execute("SELECT count(*) FROM sqlite_master")   # 密钥对了才不抛异常
```
或
```python
cur.execute("SELECT count(*) FROM message")
```

**「file is not a database」= 密钥错误**，不是文件坏。这两者要分清。

---

## 四、数据库修复原理

### 为什么不能直接 dump / recover

坏页会让 SQLite 在遍历 B-tree 时抛错并终止整个操作：
- `.dump` —— 遇到坏页整体失败
- `.recover` —— 需要 sqlite3 CLI 支持 SQLCipher，且对严重断裂的 B-tree 效果有限
- `VACUUM` / `REINDEX` —— 同样会遍历全表，失败

### 分批读取策略（本技能采用）

核心思想：**把大范围查询拆成小范围，坏页只污染它所在的那个小批次，其余数据照样读出来。**

```
for 每张表:
    从 sqlite_master 复制建表 SQL 到新库
    找主键列（PRAGMA table_info 中 pk > 0）
    取 min(pk), max(pk)
    按 500 一批遍历:
        try:  SELECT ... WHERE pk BETWEEN a AND b  → 写入新库
        except: 降批到 50 再试
                 仍失败 → 逐条试
                 仍失败 → 记录并跳过（这部分数据确实读不出来）
复制所有索引定义
新库跑 PRAGMA integrity_check
```

**要点**：
- 新库用**同一密钥**加密，微信才能打开
- `INSERT OR IGNORE` 避免主键冲突中断
- 每批 commit，避免事务过大
- 无主键的表（配置类小表）直接整表读
- 实战恢复率：3.1GB 库、151 万条消息，坏批次占比 < 1%

### 关键表

| 表名 | 内容 | 重要度 |
|------|------|--------|
| `message` | 聊天消息主体 | ★★★★★ |
| `rcontact` | 联系人 | ★★★★★ |
| `chatroom` | 群聊信息 | ★★★★☆ |
| `chatroom_member` | 群成员 | ★★★☆☆ |
| `img_flag` / `ImgInfo2` | 图片索引 | ★★★☆☆ |
| `voiceinfo` | 语音索引 | ★★☆☆☆ |
| `conversation` | 会话列表 | ★★★★☆ |
| `userinfo` | 账号信息 | ★★★★☆ |

**FTS5 全文索引表可以不恢复**——微信会重建。强行复制损坏的 FTS5 索引反而可能再次触发完整性检查失败。
打包时直接跳过 `*FTS5*` 相关文件。

---

## 五、恢复后必做的两件事

**1. 同步 salt 到 .sm**
```python
new_salt = open('EnMicroMsg_recovered.db','rb').read(16)
sm = bytearray(open('EnMicroMsg_corrupted.db.sm','rb').read())
sm[12:28] = new_salt
open('EnMicroMsg_recovered.db.sm','wb').write(bytes(sm))
```

**2. 完整性验证**
```python
cur.execute("PRAGMA integrity_check")
assert cur.fetchall() == [('ok',)]
```
只有返回单行 `ok` 才算通过。返回一堆 `Page N: btree corruption` 说明新库也有问题，
需检查写入过程（通常是磁盘满或中途中断）。
