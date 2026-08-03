# MIUI 备份格式规范与 tar 手术

---

## 一、文件结构

```
微信(com.tencent.mm).bak
├── [0 .. 65]      MIUI 文本头部，严格 66 字节
└── [66 .. EOF]    GNU tar 数据流
    ├── 512B tar 头 + 数据（512 对齐填充）
    ├── ...
    └── 两个 512 字节全零块（end-of-archive）
```

### MIUI 头部（66 字节，逐行）
```
MIUI BACKUP\n          ← magic
2\n                    ← 备份格式版本
com.tencent.mm 微信\n   ← 包名 + 应用名（UTF-8，"微信" 占 6 字节）
102\n                  ← 应用 versionCode（按实际）
0\n
ANDROID BACKUP\n
5\n
0\n
none\n                 ← 压缩方式（none = 不压缩）
```

Python 字面量：
```python
MIUI_HEADER = (
    b'MIUI BACKUP\n'
    b'2\n'
    b'com.tencent.mm \xe5\xbe\xae\xe4\xbf\xa1\n'
    b'102\n0\n'
    b'ANDROID BACKUP\n'
    b'5\n0\nnone\n'
)
assert len(MIUI_HEADER) == 66
```

> ⚠️ 若手术的**输入是已剥离头部的纯 tar**，输出时必须自己补写这 66 字节；
> 若输入是完整 `.bak`，则读走前 66 字节后原样写出。**先判断再处理**，不要假设。
> 实战踩坑：从手机 `/sdcard/temp_bak/wechat.tar` 拉的是纯 tar（无 MIUI 头），
> 而 `.bak` 是带头的，混淆后生成的文件直接不可用。

### 判定方法
```bash
head -c 32 <file> | xxd
```
- 开头是 `MIUI BACKUP` → 完整 `.bak`
- 偏移 257 处是 `ustar` → 纯 tar

---

## 二、GNU tar 头字段（512 字节）

| 偏移 | 长度 | 字段 | 说明 |
|------|------|------|------|
| 0 | 100 | name | 文件名（超长时用前置 `L` 类型条目） |
| 100 | 8 | mode | 八进制权限 |
| 108 | 8 | uid | **必须保持原值** |
| 116 | 8 | gid | **必须保持原值** |
| 124 | 12 | size | 八进制文件大小，替换文件时改这里 |
| 136 | 12 | mtime | 修改时间 |
| 148 | 8 | chksum | 校验和，改 size 后必须重算 |
| 156 | 1 | typeflag | `0`普通 `5`目录 `L`GNU长文件名 |
| 257 | 6 | magic | **`ustar\0`（GNU）**，不能是 `ustar  `（POSIX） |
| 263 | 2 | version | `00` |

### checksum 算法
```python
def compute_checksum(header):
    chk = 0
    for i in range(512):
        chk += 0x20 if 148 <= i < 156 else header[i]   # chksum 字段视为空格
    return chk

def update_header_size(header, new_size):
    h = bytearray(header)
    h[124:136] = f'{new_size:011o}\0'.encode('ascii')
    chk = compute_checksum(h)
    h[148:156] = f'{chk:06o}\0 '.encode('ascii')       # 注意结尾是 \0 + 空格
    return bytes(h)
```

### 512 字节对齐
```python
def pad_size(size):
    return size if size % 512 == 0 else size + (512 - size % 512)
```

---

## 三、tar 内的路径约定

```
apps/com.tencent.mm/a/...   → /data/data/com.tencent.mm/...          （内部数据）
apps/com.tencent.mm/r/...   → /sdcard/Android/data/com.tencent.mm/...（外部数据）
apps/com.tencent.mm/_manifest
```

目标数据库典型路径：
```
apps/com.tencent.mm/r/MicroMsg/<32位hash>/EnMicroMsg.db
apps/com.tencent.mm/r/MicroMsg/<32位hash>/EnMicroMsg.db.sm
apps/com.tencent.mm/r/MicroMsg/<32位hash>/EnMicroMsg.db.li
apps/com.tencent.mm/r/MicroMsg/<32位hash>/EnMicroMsg.db.ini
apps/com.tencent.mm/r/MicroMsg/<32位hash>/corrupted/EnMicroMsg.db   ← 要跳过
```

---

## 四、手术规则

| 条目 | 处理 |
|------|------|
| `**/EnMicroMsg.db`（非 corrupted 下） | **替换**为修复库，改 size + 重算 chksum |
| `**/EnMicroMsg.db.sm` | **替换**为更新 salt 后的 .sm |
| `**/EnMicroMsg.db.li` | **替换**为原 corrupted 版本的 .li |
| `**/EnMicroMsg.db.ini` | **替换**为原 corrupted 版本的 .ini |
| `**/corrupted/**` | **跳过**（不写入输出，避免微信再次发现坏库） |
| `*.db-wal` / `*.db-shm` | **跳过**（WAL 残留会覆盖新库内容） |
| 含 `FTS5`/`fts5` 的文件 | **跳过**（微信会自行重建索引） |
| `typeflag == 'L'` | **原样复制**头+数据，不可丢弃 |
| 其余全部 | **原样字节复制** |
| 末尾两个全零块 | **原样写出** |

### 四条格式铁律

1. **保持 GNU 格式**：不要用 Python `tarfile` 重新打包——它默认输出 POSIX（`ustar  \0`），
   MIUI 备份 App 会直接判定「备份文件损坏」。必须**手工流式逐条搬运**原始 512 字节头。
2. **保持 UID/GID**：`tarfile` 重打包会把 uid/gid 改成当前用户（如 10261/1023），
   而原值可能是 6100/10341。改了同样报损坏。
3. **保留 GNU long name（typeflag `L`）条目**：微信数据里路径很长，这类条目很多，丢一个整包就废。
4. **checksum 必须重算**，且 148–155 位在计算时按空格处理。

---

## 五、descript.xml

与 `.bak` 同目录，备份 App 用它做校验。**四个尺寸字段必须分别计算**——
算错的典型症状是：**恢复进度到 98% 后突然跳 100% 并提示「备份文件损坏」**。

| 字段 | 计算方式 |
|------|----------|
| `bakFileSize` | `.bak` 文件的实际字节数 |
| `pkgSize` | tar 内**所有文件数据字节之和**，不含 512 字节头、不含对齐填充、不含 `L` 类型条目的数据 |
| `size` | `bakFileSize + 65536` |
| `transingTotalSize` | `= pkgSize` |
| `completedSize` | `= pkgSize` |
| `date` | 13 位毫秒时间戳，需与目录名日期一致 |

### pkgSize 计算
```python
pkgSize = 0
with open(bak, 'rb') as f:
    f.seek(66)                       # 跳过 MIUI 头
    while True:
        hdr = f.read(512)
        if len(hdr) < 512 or hdr == b'\0'*512:
            break
        s = hdr[124:136].rstrip(b'\x00 ').decode('ascii')
        size = int(s, 8) if s else 0
        tf = chr(hdr[156]) if hdr[156] else '0'
        padded = size + (512 - size % 512) if size % 512 else size
        if tf == 'L':                # long name 条目不计入 pkgSize
            f.seek(padded, 1)
            continue
        pkgSize += size
        f.seek(padded, 1)
```

### 目录与日期一致性
备份目录名格式 `yyyyMMdd_HHmmss`，`descript.xml` 里的 `date`（毫秒时间戳）应与之对应，
否则备份列表里会显示错误时间，用户难以分辨要恢复哪一条。

> 小技巧：若手机上存在多条时间相近的备份，把不用的目录改名加 `.hidden` 后缀先隐藏，
> 恢复完成后再改回，避免用户选错。

---

## 六、恢复行为说明

- 进度到 **98% 后短暂停顿再跳 100%** 属正常，此时在写大文件
- 若提示「备份文件损坏」但**数据实际已写入**：多半是 `descript.xml` 尺寸字段有误，
  数据本身没问题——让用户先看微信里记录是否回来了
- 若数据出现后**又消失**：不是打包问题，是**数据库本身仍有损坏**，
  回到修复阶段重做，并确保通过 `integrity_check` 门禁
