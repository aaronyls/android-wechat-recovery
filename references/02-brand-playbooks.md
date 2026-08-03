# 品牌回传方案矩阵

修复好的数据库要写回微信的**应用私有目录**（`/data/data/com.tencent.mm/`），而非 root 的安卓无法直接写入。因此需要借助各品牌的「备份/恢复」通道作为载体。本文档给出各品牌的可行路径。

---

## 决策树

```
设备是否已 root / 有 Magisk？
├─ 是 → 【方案 A】直接 push（最简单，跳过所有备份包重建）
└─ 否 → 品牌是什么？
   ├─ 小米/Redmi (MIUI/HyperOS) → 【方案 B】MIUI 本地备份手术  ★主路径，已实战验证
   ├─ 华为/荣耀              → 【方案 C】手机克隆/备份 App（格式需先探测）
   ├─ OPPO/vivo/三星/其他    → 【方案 D】adb backup/restore（Android 12+ 常被阉割）
   └─ 均不可行               → 【方案 E】降级方案：只导出不回写
```

---

## 方案 A：已 root —— 直接 push（★★★★★）

最可靠，绕过一切备份格式问题。

```bash
# 1. 停微信
adb shell am force-stop com.tencent.mm

# 2. 记录原文件的 owner 和权限（关键！）
adb shell "su -c 'ls -ln /data/data/com.tencent.mm/MicroMsg/<hash>/EnMicroMsg.db'"
# 输出形如：-rw-rw---- 1 10261 10261 ... 记下 uid:gid

# 3. push 到中转目录再用 su 移动
adb push EnMicroMsg_recovered.db /data/local/tmp/
adb push EnMicroMsg_recovered.db.sm /data/local/tmp/

adb shell "su -c '
  cd /data/data/com.tencent.mm/MicroMsg/<hash>/
  rm -f EnMicroMsg.db-wal EnMicroMsg.db-shm
  rm -rf corrupted/
  cp /data/local/tmp/EnMicroMsg_recovered.db    ./EnMicroMsg.db
  cp /data/local/tmp/EnMicroMsg_recovered.db.sm ./EnMicroMsg.db.sm
  chown 10261:10261 EnMicroMsg.db EnMicroMsg.db.sm
  chmod 660 EnMicroMsg.db EnMicroMsg.db.sm
  restorecon EnMicroMsg.db EnMicroMsg.db.sm
'"

# 4. 清理中转文件
adb shell "rm -f /data/local/tmp/EnMicroMsg_recovered.db*"
```

**三个必须做对的点**：
1. `chown` 成微信的 uid:gid（每台设备不同，从原文件读）
2. `chmod 660`
3. `restorecon` 恢复 SELinux 上下文，否则微信仍读不到

---

## 方案 B：小米 MIUI / HyperOS 本地备份手术（★★★★★，主路径）

**这是本技能默认且已实战验证成功的路径。**

### 备份文件位置
```
/sdcard/MIUI/backup/AllBackup/<yyyyMMdd_HHmmss>/
├── 微信(com.tencent.mm).bak     ← 数据本体
└── descript.xml                  ← 元数据（尺寸校验）
```

### 文件格式
```
[66 字节 MIUI 文本头][GNU tar 数据 ...][两个 512 字节全零块]
```

MIUI 头部内容（严格 66 字节）：
```
MIUI BACKUP\n
2\n
com.tencent.mm 微信\n
102\n
0\n
ANDROID BACKUP\n
5\n
0\n
none\n
```

tar 内部路径前缀：
- `apps/com.tencent.mm/a/` —— 应用内部数据（`/data/data/...`）
- `apps/com.tencent.mm/r/` —— 外部存储数据（`/sdcard/Android/data/...`）

### 流程
1. 手机上创建微信本地备份 →  `.bak` 文件生成
2. `adb exec-out cat` **一次性**拉到电脑
3. 本地做 tar 手术（替换数据库、跳过 corrupted/WAL/FTS5）
4. 重算 `descript.xml` 四个尺寸字段
5. push 回手机新建的备份目录
6. 备份 App 里选择该备份 → 恢复

详见 `04-miui-backup-format.md`。

### HyperOS 差异
HyperOS（小米澎湃）沿用同一格式，路径可能变为
`/sdcard/MIUI/backup/AllBackup/` 或 `/sdcard/backup/AllBackup/`，先用
`adb shell "find /sdcard -maxdepth 4 -name 'descript.xml' 2>/dev/null"` 定位。

---

## 方案 C：华为 / 荣耀（★★★☆☆）

华为「备份恢复」App 与「手机克隆」使用私有格式，且部分版本对备份内容做了签名/校验，
手术难度高于 MIUI。

**探测步骤**：
```bash
adb shell "ls -la /sdcard/Huawei/Backup/ 2>/dev/null"
adb shell "ls -la /sdcard/backup/ 2>/dev/null"
adb shell "find /sdcard -maxdepth 3 -iname '*backup*' -type d 2>/dev/null"
```

找到备份文件后先读头部判定格式：
```bash
adb exec-out "dd if=<备份文件> bs=1 count=256 2>/dev/null" | xxd | head -16
```
- 若同样是 `tar`（magic `ustar`）→ 可套用方案 B 的手术逻辑（跳过 MIUI 66 字节头）
- 若为 zip / 私有加密容器 → 手术不可行，转方案 D 或 E

> 华为备份常见 `.db` + `.tar` 分片结构，且有 `info.xml` 记录校验值，改动后需同步更新。
> 若无把握，**优先建议用户临时 root 或走方案 E**。

---

## 方案 D：adb backup / restore（★★☆☆☆）

标准安卓机制，但 **Android 12 起 Google 已默认禁用**，且微信 manifest 多半设置了
`android:allowBackup="false"`，成功率低。

```bash
# 备份（手机上需点「备份我的数据」）
adb backup -f wechat.ab -noapk com.tencent.mm

# 检查是否真的有数据（文件 <1KB 说明被拒绝）
ls -la wechat.ab

# .ab 格式 = 24 字节文本头 + zlib 压缩的 tar
# 解包
dd if=wechat.ab bs=1 skip=24 | python -c "import zlib,sys;sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))" > wechat.tar

# 修改 tar 后重新打包
python -c "
import zlib,sys
head=open('wechat.ab','rb').read(24)
data=open('wechat_new.tar','rb').read()
open('wechat_new.ab','wb').write(head+zlib.compress(data))
"

# 恢复
adb restore wechat_new.ab
```

**失败判据**：`wechat.ab` 只有几百字节 → 应用禁止备份，此路不通。

---

## 方案 E：降级方案 —— 只导出不回写（★★★★☆ 实用度）

当无法回写到手机时，**至少保住数据可读**。这往往是更现实的选择：

1. 在电脑上用修复好的 `EnMicroMsg_recovered.db` 导出为**可读格式**：
   - 导出为 HTML / CSV 聊天记录存档
   - 用开源工具（如 WechatExporter、留痕/MemoTrace 等）生成带图文的浏览页面
2. 需要图片/语音/视频时，一并从手机拉取：
   ```
   /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/image2/
   /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/voice2/
   /sdcard/Android/data/com.tencent.mm/MicroMsg/<hash>/video/
   ```
3. 告知用户：手机端微信里看不到旧记录，但**完整记录已在电脑上可查**

**务必主动向用户说明这个方案的存在**——很多用户的真实诉求是「能看到过去的聊天」，
而不是「必须在手机微信 App 里看到」。当方案 C/D 风险高时，E 是更稳妥的交付。

---

## 通用注意事项

- 无论走哪个方案，**回传前必须通过 S7 验证门禁**
- 回传只做一次，做之前把所有校验做完
- 保留最终的备份包作为**回滚点**，微信若再出问题可直接重做恢复
- 恢复完成前不要打开微信
