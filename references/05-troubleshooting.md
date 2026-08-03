# 全流程故障速查

按流程阶段排列。每条给出**现象 → 根因 → 解法**。

---

## 阶段：诊断

### Q1 `corrupted/` 目录不存在，但聊天记录确实没了

不是本技能场景。逐项排查：

| 可能 | 检查 |
|------|------|
| 微信多开/分身 | `adb shell "pm list packages \| grep tencent"` 看是否有 `com.tencent.mm` 之外的克隆包 |
| 多账号 | `MicroMsg/` 下有多个 32 位 hash 目录，每个对应一个账号 |
| 数据在内部存储而非外部 | 内部路径 `/data/data/com.tencent.mm/MicroMsg/`（需 root 才能看） |
| 微信自己已「修复」过 | 检查 `MicroMsg/<hash>/` 下有无 `.db.bak`、`recover*` 之类残留 |
| 真的被清除了 | 检查 `EnMicroMsg.db` 大小，若只有几百 KB 且无 corrupted → 需要走文件级恢复（难度高，成功率低） |

```bash
# 一次性看全貌
adb shell "du -sh /sdcard/Android/data/com.tencent.mm/MicroMsg/*/ 2>/dev/null"
```

### Q2 `MicroMsg/` 下有多个 hash 目录，不知道该恢复哪个

- 按体积排序，最大的通常是主账号
- 每个目录下的 `system_config_prefs.xml` 里有 `default_uin`，可对应到账号
- **直接问用户**丢失记录的是哪个微信账号（主号/小号/工作号）

### Q3 `/sdcard/Android/data/` 访问被拒（Android 11+）

Android 11+ 对 `Android/data` 有访问限制，但 **adb shell 通常不受影响**。
若确实读不到：
```bash
adb shell "ls /sdcard/Android/data/com.tencent.mm/"   # 试试直接列
adb shell "run-as com.tencent.mm ls"                   # 仅 debuggable 应用可用（微信不行）
```
仍失败则需 root，或改用 MIUI 备份包取数据（备份包里含完整数据）。

---

## 阶段：密钥

### Q4 提示 `file is not a database`

**这是密钥错误，不是文件损坏。** 按 `03-key-and-repair.md` 的候选矩阵逐个试：
换 IMEI1/IMEI2、换 android_id、换 `cipher_compatibility` 1/3/4。

### Q5 找不到 UIN

1. 外部存储 `system_config_prefs.xml` 里搜 `default_uin`
2. 从 MIUI 备份 tar 里流式提取 `shared_prefs/auth_info_key_prefs.xml`
3. 微信「我 → 设置 → 关于微信 → 版本号连点」有时能看到调试信息
4. 兜底：`get_wechat_key.py --brute --uin-file <候选列表>`

### Q6 `sqlcipher3` 装不上

| Python | 做法 |
|--------|------|
| ≤ 3.11 | `pip install sqlcipher3-binary` |
| 3.12 / 3.13 | `pip install sqlcipher3`（需 VS Build Tools / gcc），或 `pip install pysqlcipher3` |
| 都不行 | 下载 SQLCipher CLI，用 `sqlcipher` 命令行操作；或临时装一个 3.11 虚拟环境 |

> 实战：`sqlcipher3-binary` 在 Python 3.13 上**没有轮子**，会直接报
> `No matching distribution found`。别在这里反复重试，直接按表换方案。

---

## 阶段：修复

### Q7 修复过程中报磁盘空间不足

事前用 `S ≈ 2.2×T + 1.1×R` 估算并做磁盘调度（见 SKILL.md S3）。
已经发生时：优先把工作目录整体迁到空间充足的盘，**不要靠删中间文件硬撑**——
删掉的很可能是后面还要用的输入。

### Q8 `integrity_check` 在新库上仍然报错

新库是全新写的，正常必然 `ok`。若报错，说明：
- 写入过程中磁盘满了 → 换盘重跑
- 写入被中断 → 重跑
- 复制索引时把损坏的索引定义也带过来了 → 跳过索引重建，让微信自己建

### Q9 恢复出来的消息数明显偏少

- 检查是否只处理了 `message` 一张表，其他表漏了
- 检查降批逻辑是否生效（坏批次应降到 50 再到逐条，而不是整批丢弃）
- 打印失败批次数量，若占比 > 5%，说明损坏严重，考虑对失败区间做逐条重试

---

## 阶段：打包与回传

### Q10 备份 App 提示「备份文件损坏」

按可能性排序：

| 原因 | 检查 |
|------|------|
| descript.xml 尺寸字段算错 | 四个字段分别核对（见 `04-miui-backup-format.md`） |
| tar 变成了 POSIX 格式 | `xxd` 看偏移 257，必须是 `ustar\0` 而非 `ustar  ` |
| UID/GID 被改 | 对比原始 tar 与新 tar 同一条目的 108–123 字节 |
| MIUI 66 字节头缺失或重复 | `head -c 16` 应为 `MIUI BACKUP` |
| 丢了 GNU long name 条目 | 手术脚本必须处理 `typeflag == 'L'` |
| end-of-archive 缺失 | 文件末尾应有两个 512 字节全零块 |

### Q11 恢复到 98% 跳 100% 报损坏，但数据其实进去了

典型的 `pkgSize` 不匹配。备份 App 用 `pkgSize` 校验已恢复的数据量，
数据写完了但计数对不上，于是报错。**数据是好的**——让用户先看微信。
下次生成时修正 `pkgSize` 即可消除该提示。

### Q12 数据短暂出现后又消失，微信弹「数据文件损坏」

**最关键的一类问题。** 说明回传的库内部仍有损坏：微信能读一部分，随后
`integrity_check` 失败 → 再次把库移进 `corrupted/` → 建空库。

解法：**回到修复阶段**，确保：
1. 新库 `PRAGMA integrity_check` 返回单行 `ok`
2. 打包时跳过了 `corrupted/`、`-wal`、`-shm`、FTS5
3. `.sm` 的 salt 已同步为新库的 salt

**这就是为什么 S7 验证门禁是硬性的。** 跳过它的代价是一整轮返工（打包 25min + 回传 15min + 手机恢复 20min）。

### Q13 push 大文件中途失败

- 换原装数据线 + 主板后置 USB 口
- 电源计划改「高性能」，关闭「USB 选择性暂停」
- 电脑禁止休眠
- 手机保持屏幕常亮、插电
- 失败后重新 push 前先 `adb shell rm` 掉不完整的文件，避免残留

### Q14 手机存储不足，放不下回传的备份

- 先清掉手机上的临时目录和旧备份
- 或改走 root 直推方案（无需备份包）
- 或先在手机上删掉大体积的非关键微信缓存（视频/朋友圈缓存），恢复后会自动重建

---

## 阶段：善后

### Q15 恢复成功后微信提示要「重新登录」

正常。数据库恢复不影响登录态时不会提示；若提示，正常登录即可，聊天记录仍在。
**登录后先不要点微信的「修复聊天记录」**，避免它再次触发检查。

### Q16 恢复成功但图片/语音打不开

图片、语音、视频是独立文件，不在数据库里。确认这些目录也在：
```
MicroMsg/<hash>/image2/   voice2/   video/   emoji/
```
若打包时误跳过了它们，需要重新打包或单独用 `adb push` 补回外部存储部分。

### Q17 什么时候可以删 `corrupted/` 目录

**只有在用户明确确认「聊天记录已恢复且稳定使用一段时间」之后。**
在此之前它是唯一的数据源，删了不可逆。建议先把它拉到电脑归档，再删手机上的。
