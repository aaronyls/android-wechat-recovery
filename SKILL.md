---
name: android-wechat-recovery
description: 安卓手机微信聊天记录数据库损坏后的全流程引导式恢复。适用于微信突然丢失全部聊天记录、自带「修复聊天记录」无效、但存储空间显示数据仍在的情况（EnMicroMsg.db 内部坏页 → 微信移入 corrupted/ 并建空库）。本技能从零引导用户：确认手机品牌 → 开启开发者模式与 USB 调试 → 连接 ADB → 诊断损坏类型 → 磁盘空间预判与工作盘调度 → 安全备份 → 一次性拉取数据到电脑 → 电脑端解密修复数据库 → 完整性门禁验证 → 重建备份包 → 一次性回传恢复 → 用户确认成功 → 清理手机与电脑的全部过程文件。触发词：微信聊天记录丢失、微信聊天记录消失、微信聊天记录恢复、安卓微信数据恢复、EnMicroMsg.db 损坏、微信数据库损坏、corrupted 目录、微信修复聊天记录无效、微信提示数据文件损坏
version: 2.0.0
author: WorkBuddy
agent_created: true
tags:
  - 微信
  - 聊天记录恢复
  - Android
  - SQLCipher
  - ADB
  - 数据库修复
  - MIUI备份
---

# 安卓微信聊天记录数据库损坏恢复（引导式全流程）

## 0. 这个技能解决什么问题

**典型症状**（三条同时成立即高度匹配）：
1. 微信聊天记录突然全部消失，只剩最近几条或完全空白
2. 微信自带「设置 → 帮助与反馈 → 修复聊天记录」无效，或修复后数据又消失
3. 手机「存储空间」里微信仍占用几十 GB，数据并没有真的被删

**根本原因**：微信用 SQLCipher 加密的 `EnMicroMsg.db` 存聊天记录。当数据库内部出现坏页 / B-tree 断裂时，微信启动时的 `PRAGMA integrity_check` 失败，会把整个库**移动**到 `MicroMsg/<hash>/corrupted/` 子目录，然后新建一个空库。**数据没丢，只是被搬走了。**

**核心难点**：不能简单把 corrupted 库搬回去——微信一开机又会检测到损坏再次清空。必须**在电脑上把库修好**（剔除坏页、重建干净加密库），验证 `integrity_check` 全部通过后再回传。

---

## 1. 执行总则（Agent 必读）

这套流程涉及数十 GB 数据、用户唯一的聊天记录、以及手机与电脑双端的破坏性操作。执行时**必须**遵守：

| 原则 | 含义 |
|------|------|
| **P1 电脑端闭环** | 手机只做两次大文件 I/O：**拉 1 次 + 推 1 次**。所有解密、修复、打包都在电脑本地文件上完成，禁止反复从手机流式读取同一份大文件 |
| **P2 先验后传** | 修好的库必须通过 `integrity_check` 门禁才允许回传。**绝不把未验证的数据推回手机** |
| **P3 空间先算后做** | 任何写入前先估算需求、扫描各磁盘，不足则**询问用户**是否切换工作盘，绝不"边做边腾空间" |
| **P4 二进制必须 exec-out** | 所有二进制拉取用 `adb exec-out "cat …" > file`；`adb shell cat` 会经 PTY 转换损坏数据 |
| **P5 原始数据只读** | 手机上的 `corrupted/` 目录在整个流程结束前**不得删除**，它是唯一的数据源 |
| **P6 清理需双确认** | 清理阶段必须先列出完整待删清单，得到用户明确确认后才执行；且必须在用户确认"恢复成功"之后 |
| **P7 长任务报进度** | 拉取 / 手术 / 推送等 10 分钟级任务，必须分阶段打印预期耗时与实时进度 |

**交互门（Gate）**：流程中有 6 处必须停下来问用户，用 `AskUserQuestion` 提问，不要自行假设：
- G1 手机品牌与型号确认
- G2 工作盘选择（空间不足时）
- G3 手机端创建备份（人工操作）
- G4 触发备份恢复（人工操作）
- G5 恢复结果确认（成功/失败）
- G6 清理确认

---

## 2. 流程总览

```
S0 环境与依赖预检 ─────────── scripts/preflight.py
S1 引导开启开发者模式+USB调试 ─ references/01-device-setup.md   【G1】
S2 设备探测与品牌分支 ───────── scripts/device_probe.py + references/02-brand-playbooks.md
S3 损坏诊断与空间预判 ───────── scripts/diagnose_wechat.py       【G2】
S4 安全备份 + 一次性拉取 ────── scripts/pull_wechat_data.py      【G3】
S5 获取并验证 SQLCipher 密钥 ── scripts/get_wechat_key.py
S6 电脑端修复数据库 ─────────── scripts/recover_database.py
S7 验证门禁（硬性拦截） ──────── scripts/verify_gate.py
S8 重建回传载体 ─────────────── scripts/miui_backup_surgery.py + make_descript.py
S9 一次性回传并恢复 ─────────── adb push                        【G4】【G5】
S10 归档 + 双端清理 ─────────── scripts/cleanup.py               【G6】
```

**预计总耗时**：数据量 30GB 级约 2–3 小时，其中拉取 15 分钟、修复 20–40 分钟、打包 25 分钟、回传 15 分钟。

---

## 3. 逐步执行

### S0 · 环境与依赖预检

```bash
python scripts/preflight.py
```

检查并报告：ADB 是否可用、Python 版本、`sqlcipher3` 是否可导入、各磁盘剩余空间。

**Python 版本 → SQLCipher 包对照**（预检失败时按此安装）：

| Python | 推荐包 | 备注 |
|--------|--------|------|
| ≤ 3.11 | `pip install sqlcipher3-binary` | 有预编译轮子，最省事 |
| 3.12 / 3.13 | `pip install sqlcipher3` | 需源码编译，或改用 `pysqlcipher3` |
| 任意 | SQLCipher CLI | 兜底：下载官方命令行工具 |

> 实战教训：`sqlcipher3-binary` 在 Python 3.13 上无轮子，会直接安装失败。**不要在这里反复试**，按表选包。

若 ADB 不存在，引导用户下载 Google 官方 platform-tools（见 `references/01-device-setup.md`）。

---

### S1 · 引导开启开发者模式与 USB 调试 【G1】

**先用 `AskUserQuestion` 问清品牌**（不同品牌路径差异很大）：

> 问：您的手机是什么品牌？
> 选项：小米/Redmi · 华为/荣耀 · OPPO/一加/realme · vivo/iQOO · 三星 · 其他

拿到品牌后，从 `references/01-device-setup.md` 取对应的**分步操作指引**，逐条念给用户，包括：
1. 开启开发者选项（连点「版本号」7 次的具体位置因品牌而异）
2. 开启 USB 调试（部分品牌还需开「USB 安装」「USB 调试(安全设置)」）
3. 数据线连接电脑，选择「传输文件 / MTP」模式
4. 手机弹出「允许 USB 调试吗？」→ 勾选「一律允许」→ 确定

验证连接：
```bash
adb devices -l
```
出现 `device` 状态即成功。出现 `unauthorized` / `offline` / 无设备，按 `references/01-device-setup.md` 的排障表处理。

---

### S2 · 设备探测与品牌分支

```bash
python scripts/device_probe.py
```

输出：品牌 / 型号 / Android 版本 / 系统版本 / 微信版本 / 手机可用存储 / 是否 root。

**根据品牌选择回传方案**（详见 `references/02-brand-playbooks.md`）：

| 品牌 | 回传载体 | 可行性 |
|------|----------|--------|
| 小米 / Redmi（MIUI / HyperOS） | **MIUI 本地备份 `.bak` 手术**（本技能主路径，已实战验证） | ★★★★★ |
| 华为 / 荣耀 | 手机克隆 / 备份 App，格式不同需先探测 | ★★★☆☆ |
| OPPO/vivo/三星等 | 优先 `adb backup/restore`（Android 12+ 多被阉割）；否则需 root | ★★☆☆☆ |
| 任意品牌 + 已 root | **直接 `adb push` 回私有目录**，最简单可靠 | ★★★★★ |

> **重要**：若设备已 root，跳过 S8 的备份包重建，直接 push 文件到
> `/data/data/com.tencent.mm/MicroMsg/<hash>/` 并修正 owner/权限即可。
> 非 root 的小米设备走 MIUI 备份手术路径（本技能默认路径）。

---

### S3 · 损坏诊断与空间预判 【G2】

```bash
python scripts/diagnose_wechat.py
```

脚本做三件事：

**a) 定位损坏库**
```bash
adb shell "ls /sdcard/Android/data/com.tencent.mm/MicroMsg/ | grep -E '^[a-f0-9]{32}$'"
adb shell "ls -la /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/corrupted/"
```
- `corrupted/EnMicroMsg.db` 存在且体积大 → **确诊，继续**
- 不存在 → 不是本技能场景，检查是否为「换机未迁移」「多开分身」「账号不同」，见 `references/05-troubleshooting.md`
- 有多个 hash 目录 → 是多微信账号，用体积最大的那个，或问用户丢失的是哪个账号

**b) 估算空间需求**

设 `T` = 手机上微信总数据量（≈备份包大小），`R` = corrupted 库大小：

```
本地最低空闲 S ≈ 2.2 × T + 1.1 × R
```

来源：输入 tar(T) + 输出 .bak(≈T) + 恢复库(R) + 10% 缓冲。
> 实战：T=32GB、R=3.1GB → 需 **≈74GB**。当时工作盘只有 27.5GB，直接导致中途磁盘满返工。

**c) 磁盘调度**

脚本遍历所有固定盘，若当前工作盘 `Free < S`，**必须用 `AskUserQuestion` 询问用户**：

> 当前工作盘 C: 剩余 27.5 GB，本次至少需要 74 GB。
> 检测到 D: 剩余 680 GB。是否切换到 D: 盘处理？
> 选项：切换到 D:（推荐） · 留在 C: 我自行清理 · 换其他盘

**手机端空间也要判**：回传 `.bak` 需手机额外有 `≈T` 空闲。若不足，改走"直接拉现成备份改完推回"，不在手机端新建备份。

---

### S4 · 安全备份 + 一次性拉取 【G3】

**a) 先拉走原始损坏库（最重要的一步，务必先做）**

```bash
python scripts/pull_wechat_data.py --hash <hash> --out <workdir>
```

用 `adb exec-out` 拉取这 4 个文件并**逐个比对大小**：

| 文件 | 作用 |
|------|------|
| `EnMicroMsg.db` | 聊天记录主库（加密） |
| `EnMicroMsg.db.sm` | 安全文件，偏移 12–28 存放数据库 salt |
| `EnMicroMsg.db.li` | 索引文件 |
| `EnMicroMsg.db.ini` | 配置文件，含 `createmd5` |

> **P4 铁律**：必须 `adb exec-out "cat …" > file`。用 `adb shell cat` 会因 PTY 换行转换损坏二进制。
> 拉完立刻比对本地文件大小与 `adb shell stat -c %s` 一致，不一致就重拉。

**b) 创建回传载体（非 root 路径）**

先用 `AskUserQuestion` 告知用户需要在手机上手动操作：

> 请在手机上创建一次微信本地备份：
> **设置 → 更多设置 → 备份和重置 → 本地备份 → 新建备份 → 只勾选「微信」（含应用数据）→ 开始备份**
> 完成后告诉我。（约需 10–20 分钟，会占用手机 ≈XX GB 空间）

备份完成后，**一次性**把备份文件拉到电脑本地：
```bash
adb shell "ls -la /sdcard/MIUI/backup/AllBackup/"
adb exec-out "cat '/sdcard/MIUI/backup/AllBackup/<日期>/微信(com.tencent.mm).bak'" > <workdir>/wechat_orig.bak
```

> **P1 铁律**：拉到本地后，**后续所有手术都基于这个本地文件**。绝不再从手机流式读第二次。
> 实战教训：当时同一份 32GB tar 从手机拉了 3 次，白白浪费 45 分钟。

---

### S5 · 获取并验证 SQLCipher 密钥

```bash
python scripts/get_wechat_key.py --db <corrupted_db> --auto
```

**密钥推导（传统方案）**：`key = MD5(IMEI + UIN)[:7]`，配 `PRAGMA cipher_compatibility = 1`

**UIN 来源**（脚本会依次尝试）：
```bash
adb exec-out "cat /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/system_config_prefs.xml" | grep -o 'default_uin[^/]*'
adb exec-out "cat /data/data/com.tencent.mm/shared_prefs/auth_info_key_prefs.xml"   # 需 root
```

**IMEI 来源**：
```bash
adb shell service call iphonesubinfo 1
adb shell settings get secure android_id     # 部分版本用 android_id 代替 IMEI
```

**若传统推导失败**，按 `references/03-key-and-repair.md` 的候选矩阵暴力尝试：
`MD5(IMEI+UIN)[:7]` / `MD5(UIN+IMEI)[:7]` / `MD5(android_id+UIN)[:7]` / `.ini` 的 `createmd5` 全值 / `createmd5[:7]` / 空密钥（未加密）/ `cipher_compatibility` 取 1~4。

**验证成功的标志**：能执行 `SELECT count(*) FROM message` 并返回数字。

---

### S6 · 电脑端修复数据库

```bash
python scripts/recover_database.py <corrupted_db> <recovered_db> <key> [cipher_compat]
```

原理：坏页导致 `.dump` / `.recover` 整体失败，因此**按主键分批读取**，遇到坏批次自动降批（500 → 50 → 逐条），跳过读不出的页，把能读到的行全部写入一个**全新的干净加密库**。

- 逐表处理（`message` / `rcontact` / `chatroom` / `chatroom_member` / `img_flag` …）
- 表结构与索引从 `sqlite_master` 原样复制
- 新库用**相同密钥**加密（微信必须能用原密钥打开）
- 每 5 万行打印一次进度（P7）

完成后脚本自动跑 `PRAGMA integrity_check`。

**同步 salt**（关键，遗漏会导致微信打不开库）：
```bash
python scripts/update_sm_salt.py <recovered_db> <corrupted_sm> <recovered_sm>
```
新库有新的随机 salt（文件前 16 字节），必须写入 `.sm` 文件偏移 `12:28`，否则微信校验失败。

---

### S7 · 验证门禁（硬性拦截）

```bash
python scripts/verify_gate.py --db <recovered_db> --key <key> --src <corrupted_db>
```

**全部通过才允许进入 S8**，任何一项失败都退回 S6：

- [ ] `PRAGMA integrity_check` 返回 `ok`
- [ ] 能用原密钥打开，`cipher_compatibility` 一致
- [ ] `message` 表行数 ≥ 源库可读行数的 95%
- [ ] `rcontact` 表非空（联系人在）
- [ ] `.sm` 文件偏移 12:28 的 salt == 新库前 16 字节

> **P2 铁律**：这一关是本流程最重要的改进。实战中第一版跳过验证直接回传，结果微信读了一下就再次判定损坏并清空，白白浪费一整轮（30GB 打包 + 15 分钟回传 + 一次手机恢复）。

---

### S8 · 重建回传载体（MIUI 路径）

**备份格式**：`66 字节 MIUI 头部 + GNU tar 数据`。

首步先确认格式（避免选型踩坑）：
```bash
head -c 200 wechat_orig.bak | xxd | head -20
```
应看到 `MIUI BACKUP` 文本头，随后 512 字节对齐的 tar 条目、magic 为 `ustar\0`。
> **不要**把它当 zip 处理。实战中曾误判为 zip64，白做一轮 30GB 生成。

**a) tar 手术**
```bash
python scripts/miui_backup_surgery.py \
  --src wechat_orig.bak --out wechat_final.bak \
  --db <recovered_db> --sm <recovered_sm> --li <corrupted_li> --ini <corrupted_ini>
```

流式逐条处理 tar 条目：
- 替换 `EnMicroMsg.db` / `.sm` / `.li` / `.ini`（改 size 字段 + 重算 checksum）
- 跳过 `corrupted/` 目录、`.db-wal` / `.db-shm`、FTS5 索引文件
- 其余条目**原样字节复制**

**四条格式铁律**（违反任何一条，备份 App 都会报「备份文件损坏」）：
1. 保持 GNU tar 格式，magic 必须是 `ustar\0` + `00`（不能变成 POSIX 的 `ustar  \0`）
2. UID/GID 保持原值（实战中被改成 10261/1023 直接导致整包作废）
3. 保留 GNU long name 条目（`typeflag == 'L'`）及其数据块
4. checksum 计算时，148–155 字节视为空格 `0x20`

**b) 生成 descript.xml**
```bash
python scripts/make_descript.py --bak wechat_final.bak --template <原 descript.xml> --out descript.xml
```

四个尺寸字段**必须分别计算**（算错就是经典的"恢复到 98% 突然报损坏"）：

| 字段 | 计算方式 |
|------|----------|
| `bakFileSize` | `.bak` 文件实际字节数 |
| `pkgSize` | tar 内**所有文件数据之和**（不含 tar 头与 512 对齐填充） |
| `size` | `bakFileSize + 65536` |
| `transingTotalSize` / `completedSize` | `= pkgSize` |

脚本内置断言，不通过不出文件。

---

### S9 · 一次性回传并恢复 【G4】【G5】

```bash
# 1. 停微信，清理残留（注意：不要删 corrupted/，那是数据源）
adb shell am force-stop com.tencent.mm
adb shell "rm -f /sdcard/Android/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db-wal"
adb shell "rm -f /sdcard/Android/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db-shm"

# 2. 创建新备份目录并推送（≈15 分钟 / 30GB）
adb shell "mkdir -p /sdcard/MIUI/backup/AllBackup/<新日期>"
adb push wechat_final.bak "/sdcard/MIUI/backup/AllBackup/<新日期>/微信(com.tencent.mm).bak"
adb push descript.xml "/sdcard/MIUI/backup/AllBackup/<新日期>/descript.xml"
```

> 若手机上有多条备份记录且日期相同容易混淆，把旧备份目录改名加 `.hidden` 后缀先隐藏，恢复完再改回。

**【G4】引导用户在手机上操作**（用 `AskUserQuestion`）：
> 请在手机上：**设置 → 更多设置 → 备份和重置 → 本地备份 → 选择日期为「XX」的备份 → 勾选「微信」→ 恢复**
> 恢复完成前**不要打开微信**。完成后告诉我结果。

**【G5】结果确认**：
- 进度走到 98% 后短暂停顿再到 100% 是**正常**的
- 若报「备份文件损坏」但数据实际已写入，让用户先打开微信看聊天记录是否回来了
- 若聊天记录出现后又消失 → 说明 S7 门禁没做到位，回到 S6 重修

用 `AskUserQuestion` 确认：
> 恢复结果如何？
> 选项：聊天记录已恢复且稳定 · 数据出现后又消失 · 恢复过程报错 · 完全没有变化

**只有拿到「已恢复且稳定」，才进入 S10。**

---

### S10 · 归档 + 双端清理 【G6】

**a) 先归档回滚点**

问用户归档目录（默认建议 `D:\聊天数据恢复\`），把这些保留下来：

| 保留文件 | 理由 |
|----------|------|
| `wechat_final.bak` | **回滚点**：微信若再出问题可直接重做恢复，无需重跑全流程 |
| `EnMicroMsg_recovered.db` | 修复后的干净库 |
| `.sm`（新旧各一份） / `.li` / `.ini` | 重打包所需的辅助文件 |
| `descript.xml` | 恢复元数据 |

**b) 清理（P6：必须列清单 + 用户确认）**

```bash
# 1) 先看清单（什么都不删）
python scripts/cleanup.py --workdir <workdir> --hash <32位hash> --dry-run

# 2) 用户逐项核对清单无误后，正式清理（不含 corrupted/）
python scripts/cleanup.py --workdir <workdir> --hash <hash> \
    --bak-orig 20260801_140743 --bak-new 20260801_153000 \
    --archive "D:\聊天数据恢复" --confirm

# 3) 用户最终确认微信稳定后，再单独删手机 corrupted/（单独闸门）
python scripts/cleanup.py --workdir <workdir> --hash <hash> \
    --confirm --delete-corrupted
```

- `--bak-orig` / `--bak-new` 省略时不删对应备份目录（不误删）。
- `--delete-corrupted` 是 `corrupted/` 的**单独硬性闸门**：即使 `--confirm` 也不会动它，必须显式带上才删——它是唯一的重做资本。
- 电脑端删除优先走回收站（30GB 级大文件直接删以免回收站占满）；手机端用 `adb rm` 显式路径，杜绝 `rm -rf *.bak` 通配批删。

清理范围见 `references/06-cleanup-checklist.md`，典型包括：

*电脑端*：中间版本 `.bak`、logcat 日志、截图、一次性脚本、二进制片段、解包临时目录
*手机端*：`/sdcard/temp_*` 等临时目录、手术用的原始备份、回传用的备份目录、`corrupted/` 目录（**必须最后删，且确认成功后才删**）

> 实战数据：清理后电脑释放 29.6 GB、手机释放约 182 GB。

**清理前必须向用户展示待删清单并逐项确认。禁止对 Desktop/Downloads/Documents 等个人目录做递归删除。**

---

## 4. 参考资料索引

| 文件 | 内容 |
|------|------|
| `references/01-device-setup.md` | 各品牌开发者模式/USB 调试开启步骤、ADB 安装、连接排障表 |
| `references/02-brand-playbooks.md` | 品牌 → 回传方案矩阵（MIUI / 华为 / OPPO / vivo / 三星 / root） |
| `references/03-key-and-repair.md` | SQLCipher 密钥候选矩阵、数据库修复原理与分批策略 |
| `references/04-miui-backup-format.md` | MIUI 备份二进制格式规范、tar 手术要点、descript.xml 字段表 |
| `references/05-troubleshooting.md` | 全流程故障速查（12 个高频问题及解法） |
| `references/06-cleanup-checklist.md` | 双端清理清单与安全边界 |

## 5. 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight.py` | 环境/依赖/磁盘预检与工作盘调度建议 |
| `scripts/device_probe.py` | 设备品牌、型号、微信版本、存储探测 |
| `scripts/diagnose_wechat.py` | 定位 corrupted 库、估算空间需求 |
| `scripts/pull_wechat_data.py` | 一次性安全拉取（exec-out + 大小校验） |
| `scripts/get_wechat_key.py` | 密钥推导与验证（候选矩阵自动遍历） |
| `scripts/recover_database.py` | 分批修复到干净加密库 |
| `scripts/update_sm_salt.py` | 同步 .sm 文件 salt |
| `scripts/verify_gate.py` | 回传前硬性验证门禁 |
| `scripts/miui_backup_surgery.py` | 本地 tar 手术（支持本地文件源与 ADB 流两种模式） |
| `scripts/make_descript.py` | 计算并生成 descript.xml（内置断言） |
| `scripts/cleanup.py` | 双端清理（dry-run + 确认双保险） |

## 6. 安全与边界

- **不要在流程完成前删除手机上的 `corrupted/` 目录**——它是唯一数据源，删了就真没了
- **不要用 `adb shell cat` 传二进制**——PTY 转换会静默损坏数据
- **不要把未验证的库回传手机**——微信会再次判损并清空，且可能覆盖现有状态
- **清理必须双确认**，不对个人目录做递归删除
- 全流程需保持 USB 连接稳定，建议关闭电脑休眠
- 本技能仅用于**用户恢复自己设备上的自有数据**
