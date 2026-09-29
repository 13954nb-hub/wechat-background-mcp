# Read-store source attribution
## 简体中文摘要

本项目只将 Apache-2.0 上游项目中的只读数据库格式与账号布局信息作为实现参考，并保留其许可证；本仓库替换了上游扫描、密钥缓存和临时解密流程。压缩标记语义参考 Tencent WCDB 的 Apache-2.0 源码，解码器独立实现，并使用单独安装的 `zstandard`。本项目未打包或动态载入上述上游代码。完整来源、提交号和实现边界见下方英文归属记录。

The Config.Cipher name, XOR mask and object offsets in
`src/wxbg/readstore_keys.py` and the global_config account layout in
`src/wxbg/readstore_account.py` derive from `wechatauto/db.py` in
https://github.com/fanyuantaier/wechatauto-replica
at commit `492a8fb70b95865613d6d8d9740323233dbfa197`.

The upstream project is licensed under Apache License 2.0. Its license is
preserved at `licenses/wechatauto-Apache-2.0.txt`. No upstream NOTICE file was
found in the inspected checkout.

Our implementation replaces the upstream scanner with caller-owned read-only
I/O, a total two-pass byte budget, deadlines, bounded candidates, per-database
first-page HMAC verification, and fixed errors. It does not use upstream disk
key caching, decrypted database temporary files or master-key fallback logic.
The module is loaded normally from this package; no upstream source file is
read or dynamically executed at runtime.

The compression flag interpretation is verified against Tencent WCDB,
commit `39dd797099d41cf1953d5668acd8cb608016c599`,
`src/common/core/compression/CompressionConst.hpp` and
`src/common/core/compression/DecompressFunction.cpp`:
https://github.com/Tencent/wcdb/tree/39dd797099d41cf1953d5668acd8cb608016c599/src/common/core/compression
The decoder implements these format semantics independently and uses the
separately installed `zstandard==0.25.0` package. WCDB implementation sources
are not bundled or dynamically loaded. Dictionary-compressed bodies and
binary-object flags remain unsupported rather than guessed as text.

The V2 image container layout and cfg DWORD +0x40 key derivation semantics
were inspected in `wechatauto/media.py` and `wechatauto/db.py` at the same
Apache-2.0 commit above. The local image decoder/key lifetime implementation
is independent: strict bounded segments and PKCS7, no key persistence or
guessing, no footer trimming, and account/config revalidation. Actual owned
PNG cache contents independently verified the derived bytes. The packed-info
field 3/4 candidate locator was confirmed by local aggregate wire-shape probes;
its value is not treated as a content checksum or original-image identity.
