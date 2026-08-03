# 双端清理清单与安全边界

> **前置条件（不可跳过）**：用户已明确确认「聊天记录已恢复且可正常使用」。
> 在此之前**任何清理都不得执行**——过程文件是唯一的重试资本。

---

## 一、清理三原则

1. **先归档，后清理**：确认要保留的文件已复制到归档目录并**校验大小一致**，才允许删源文件
2. **先列清单，后执行**：向用户展示完整待删清单（路径 + 大小），获得明确确认
3. **corrupted/ 最后删**：手机上的原始损坏库是最终数据源，放在整个清理流程的最后一步，并单独确认

---

## 二、保留清单（归档，不删）

归档到用户指定目录（建议 `D:\聊天数据恢复\` 一类非系统盘位置）：

| 文件 | 体积量级 | 保留理由 |
|------|----------|----------|
| `wechat_final.bak` | ≈T（30GB 级） | **回滚点**——微信若再出问题，直接重做一次恢复即可，无需重跑全流程 |
| `EnMicroMsg_recovered.db` | ≈R（3GB 级） | 修复后的干净库，可用于二次打包或导出阅读 |
| `EnMicroMsg_recovered.db.sm` | 数十 KB | 已同步 salt，重打包必需 |
| `EnMicroMsg_corrupted.db.sm` | 数十 KB | 原始版本，作对照 |
| `EnMicroMsg_corrupted.db.li` | MB 级 | 打包必需 |
| `EnMicroMsg_corrupted.db.ini` | < 1KB | 含 createmd5 |
| `descript.xml` | < 2KB | 恢复元数据 |
| 关键脚本 3 个 | 数十 KB | 重打包用：surgery / calc sizes / recover |
| 复盘记录（可选） | KB 级 | 记录密钥、hash 目录名、尺寸值等参数 |

> **强烈建议**在归档目录放一个 `README.txt`，记下：
> 微信 hash 目录名、SQLCipher 密钥、cipher_compatibility、恢复日期、消息条数。
> 这些参数下次重做时能省掉大量摸索。

---

## 三、电脑端删除清单

| 类别 | 典型文件 | 说明 |
|------|----------|------|
| 中间版本备份包 | `wechat_new.bak`、`wechat_v1.bak` 等 | 只保留最终成功的那个 |
| 原始拉取的 tar/bak | `wechat_orig.bak`、`wechat.tar` | 若最终包已归档，原始输入可删（体积最大） |
| 损坏的原始库 | `EnMicroMsg_corrupted.db` | 3GB 级。**若空间允许建议保留**，作为最后的原始证据 |
| 日志 | `restore_logcat*.txt`、`*.log` | 常有数百 MB |
| 截图 | `*.png` 排障截图 | — |
| 一次性脚本 | `try_keys*.py`、`count_*.py`、`extract_*.py` 等 | 已被规范脚本集取代 |
| 二进制片段 | `part1.bin`、`zip_tail.bin`、`miui_header.bin` | 调试残留 |
| 旧版元数据 | `descript.xml`、`descript_new.xml` | 只留最终版 |
| 解包临时目录 | `extracted/`、`temp/` | 可能含大量小文件 |

清理后核对释放空间（实战参考：释放约 29.6 GB）。

---

## 四、手机端删除清单

**按此顺序执行**：

```bash
# 1. 临时工作目录（体积最大，优先删）
adb shell "rm -rf /sdcard/temp_bak /sdcard/temp_full /sdcard/temp_extract"

# 2. 手术用的原始备份（已在电脑归档）
adb shell "rm -f '/sdcard/MIUI/backup/AllBackup/<原日期>.zip.hidden'"
adb shell "rm -rf '/sdcard/MIUI/backup/AllBackup/<原日期>/'"

# 3. 回传用的备份目录（已在电脑归档，手机上不必留）
adb shell "rm -rf '/sdcard/MIUI/backup/AllBackup/<新日期>/'"

# 4. 恢复被隐藏的目录名（如果之前改过 .hidden）
adb shell "mv '/sdcard/MIUI/backup/AllBackup/xxx.zip.hidden' '/sdcard/MIUI/backup/AllBackup/xxx.zip'"

# 5. WAL/SHM 残留（微信会自建，删掉无害）
adb shell "rm -f /sdcard/Android/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db-wal"
adb shell "rm -f /sdcard/Android/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db-shm"

# 6. 【最后一步，单独确认】原始损坏库目录
adb shell "rm -rf /sdcard/Android/data/com.tencent.mm/MicroMsg/*/corrupted/"
```

> 第 6 步执行前必须再次向用户确认：
> 「手机上的 `corrupted/` 目录是原始数据源，占用约 X GB。
> 电脑上已归档一份。确认微信聊天记录使用正常后，是否删除手机上的这份？」

删完用 `adb shell df /sdcard` 核对释放空间（实战参考：释放约 182 GB）。

---

## 五、验证清理结果

```bash
# 手机
adb shell "ls -la /sdcard/MIUI/backup/AllBackup/"
adb shell "ls -d /sdcard/temp_* 2>/dev/null || echo 'temp dirs cleaned'"
adb shell "df -h /sdcard"

# 电脑
ls -la <workdir>
ls -lhS <归档目录>
```

并向用户报告：**清理前 → 清理后**的空间对比。

---

## 六、安全边界（红线）

- ❌ 不对 **Desktop / Downloads / Documents / 用户主目录** 做任何递归删除
- ❌ 不用通配符批删（如 `rm -rf *.bak`），必须**逐个明确路径**
- ❌ 不在用户确认恢复成功前删除任何原始数据
- ❌ 不删除微信当前正在使用的数据库文件（`EnMicroMsg.db` 本体）
- ✅ 删除前先 `ls -la` 展示，让用户看到具体路径与大小
- ✅ 分批执行，每批后验证，出错立即停止
- ✅ 电脑端优先用回收站机制（Windows 可用 PowerShell 的 Shell.Application 移入回收站），
  对超大文件（30GB 级）再考虑直接删除以避免回收站占满磁盘
