# AtlasRAG Stage 4 — Index & Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task after user approval. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start implementation or delegate work merely because this plan exists.

**Goal:** 将已持久化的 CHUNKED candidate 转为经过独立 exact-set 校验的 Milvus candidate，并以一个 PostgreSQL 事务完成 publish，接通单文档 build subgraph。

**Architecture:** PostgreSQL canonical records 是唯一数据权威；Milvus 是可重建派生索引。Representation、embedding、indexing、verification、publish 分层，整个单文档调用持有 session advisory lease，事务只包围短数据库操作。无 migration、无 Redis publish 依赖、无 Stage 5 recovery 或 Part II serving。

**Tech Stack:** Python 3.11、immutable Pydantic contracts、LangGraph runtime、现有 Psycopg UoW、PyMilvus、非默认 `indexing-embedding` 依赖组及本地 Qwen3-Embedding-0.6B。

---

## 1. 审阅结论与当前证据

状态：**用户已批准 A/B/C 并授权执行 T0 → T11（2026-10-15）。T0–T10 已提交，T10 为 b27f3e0；2026-11-09 已从保留提交恢复独立 worktree，T11 本地全量门禁全部通过，发布与最终远端 CI 进行中；尚未完成 Stage 4。**

批准限制：exact file/import/AST whitelist only，无 graph 目录通配豁免；Stage 1–3 frozen contracts 不变。LangSmith 仅允许锁定的传递 distribution，禁止直接集成/tracing/key/network requirement。ChunkInputLimitError 只用于确定性输入容量失败，不扩展 public ErrorCode，不改变 canonical chunk output/IDs。

计划审阅轮只做文档/代码审阅、远端 SHA 与 GPU 信息读取；没有创建 Stage 4 worktree、安装依赖、下载模型、执行 build、修改生产代码/测试、提交或 push。

- 权威文档为 tracked `docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`，重点对照 §10–24A、§85；不使用或修改根目录未跟踪的同名文件。
- 实时 `git ls-remote origin refs/heads/main` 返回 `4c5e6b0591286280d050c26c9df10a60ee499baa`；本地 main 相同。Stage 3 head `415cbbd200b6f00adbb9e24f38fcae9ed6ff61aa` 与 PR #4 merge commit 均是该 main 的祖先。
- Stage 3 report 中 “PR open/unmerged” 是合并前历史记录，不能覆盖上述 Git 证据；不为本次规划改写历史报告。
- `2273 passed, 47 deselected` 是已有合并后基线记录，**本轮未重新运行**；执行前必须在新 worktree 重测，以实际输出为准。
- 主机读取到 `NVIDIA GeForce RTX 3070 Laptop GPU / 8192 MiB / driver 576.28`。只证明硬件可见，没有证明 PyTorch CUDA 可用或 Qwen smoke 通过。
- 现有 `PostgresUnitOfWork` 同时暴露 documents、chunks、elements、runtime_metadata；足以用一个 commit 发布 revision 和 invalidation marker，不需要改 Repository/UoW ownership 或数据库 schema。

## 2. 已获批准的实施决策（以下保留原审阅依据）

### A. 必须批准：旧阶段 scope guards 的精确演进

Stage 4 附件 §121 明确要求：旧 frozen test 与本 Stage 冲突时 STOP / REPORT。实际冲突包括：

| 文件/检查 | 当前限制 | Stage 4 所需变化 |
| --- | --- | --- |
| `tests/contract/test_stage1_scope.py` 的 production/dependency manifests | 枚举全仓库生产文件、direct/locked distributions | 追加明确的 Stage 4 文件及锁依赖，不改成目录通配放行 |
| 同文件的 import guards | 禁止 LangGraph、torch 等后续阶段 imports | 仅批准 graph runtime 和 embedding provider 的精确层级例外 |
| 同文件 `test_graph_package_contains_only_declarative_state_modules` | 整个 graphs 树不能有函数或调用 | 保留原 Stage 1 模块的 declarative 检查；新增 indexing graph 的独立可执行边界检查 |
| `tests/stage3/test_stage3_scope.py::test_parser_dependencies_do_not_leak_into_runtime_or_dev_groups` | runtime/dev 也禁止 LangGraph | 只解除 Stage 4 正式 runtime LangGraph 的旧阶段限制；parser/model SDK 仍不得进入默认组 |

建议批准：**只演进阶段范围清单，不放宽冻结业务合同。** 保留全部旧测试及语义断言；保留 Stage 1 的 15 个 State exports/字段、Stage 2 schema/UoW、Stage 3 canonical/lineage/metadata tests。新增负向测试证明未知文件、越层 SDK、State dependency/vector、未批准包仍被拒绝。不得删除、skip、全目录豁免或修改旧 expected output 来适配错误实现。

A 已按上述限制批准；只能精确演进 guards。

### B. 必须明确：“No LangSmith”的依赖口径

官方 LangGraph manifest 引入 `langchain-core`；后者声明 `langsmith` 依赖。因此，“运行完全离线、无 key、无 tracing requirement”与“锁文件完全没有 langsmith distribution”不是一回事。[LangGraph manifest](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/pyproject.toml)、[LangChain Core manifest](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/core/pyproject.toml)。这是审阅时上游分支内容，不是本项目已 resolved 的版本证据。

建议批准：只直接声明 `langgraph` runtime；允许其必要的、完整锁定的传递依赖，但不直接集成 LangSmith、不启用 tracing/callback backend、不使用 dev server/deployment/DB checkpointer。离线 graph gate 清除 key、关闭 tracing，并禁止外连；包可安装不等于允许网络连接。若要求 distribution 也必须为零，则 STOP，不能 `--no-deps`、patch vendor 或以自制 graph 冒充 LangGraph。

### C. 建议批准：受控 CHUNK failure 的最小类型化补充

现有 `services/ingestion/chunking.py` 对“表格单行/单元格无法无损容纳”和内部算法错误都使用普通 `ValueError`。Stage 4 不能把所有 `ValueError` 都记为业务 FAILED，也不能依赖 exception message 字符串分类。

建议新增 internal `ChunkInputLimitError(ValueError)`，仅替换已确定的用户输入容量失败 raise sites，保留原消息和 ValueError 兼容性；内部 source-map/contiguity/算法错误保持传播。不改 chunk 输出、ID、profile、路由、持久化 contract 或算法版本。新增回归证明错误精确分类且既有 canonical fixtures 完全不变。若不批准该补充，不能声称 oversized 输入的 CHUNK 业务路由已完整接通。

### D. 用户已批准：whitespace-preserving index text amendment

T1 质量审查发现 Stage 3 合法的 source-backed 纯空白 Child 被 Stage 2 index text 非空白约束拒绝。停止并报告后，用户明确批准将 index text 从 `nonblank` 改为 `nonempty + whitespace-preserving`：只修改 `MilvusChunkRecord` 的 content/sparse_text 和 Stage 4 对应文本字段；空字符串仍拒绝，identifier/file_name 等仍 nonblank，其余 Unicode/NUL/字节上限检查不变。Stage 3 output、chunk IDs/content hashes、schema 全不变，无 migration。禁止 trim/drop/rechunk/sentinel/substitution。

追加硬门：T1 跨阶段回归及早期真实 Milvus record/strong-read exact-set；T2 真实 Qwen whitespace embedding；T5/T9 使用正式 verifier 的真实 Milvus whitespace exact-set 回归。若真实 Milvus 不能表达，STOP / REPORT，不做替代。

## 3. 建议采用的实现决策

### 3.1 新增 indexing 层，复用 Stage 3 primitives

新增 `domain/indexing`、`services/indexing`、`providers/embedding`、`graphs/indexing`。保留 `services/ingestion` 禁止依赖 Milvus/cache 的旧边界。应用装配位于 `application/document_build.py`，不把运行时装配塞进 Stage 1 State package 或 `runtime/__init__.py`。

不直接把现有 `parse_candidate()` 绑定到 `parse_source`：该 helper 已经调用 `persist_parsed()`，会吞并冻结的单独节点。新服务适配层按节点调用 `mark_parsing`、parser、mutation guard、`persist_parsed`、`mark_chunking`、chunker、`persist_chunked`；保留旧 helper 行为不变。

### 3.2 Representation / embedding

- CHILD：`dense_text == sparse_text == raw_content`；TABLE：二者等于已有 `render_table_slice(table_slice)`；PARENT 明确拒绝进入 builder/record/provider。
- 内部 immutable `RepresentationProfileV1` 绑定 `PartIConfig.representation_version`；使用现有字段，不改 fingerprint 算法。表示策略变更必须 bump representation version。
- 新 `RetrievalRepresentation` 和 `RetrievalRecord` 不成为 Stage 1 public exports、不入 PostgreSQL、不进 Graph State。结构化元数据使用 `IndexStructuredMetadataV1`，包含 schema version、section/source anchoring、table sheet/row/source-element facts；显式映射到现有 Milvus JSON 字段，反读重新验证。禁止 provider-native payload。
- 文本字节容量在 representation/record 边界按现有 Milvus 上限检查；token 容量在 provider 内使用实际 tokenizer 检查。全量保留文本，`truncation=False`，不缩写、不切尾、不在 embedding 层再分 chunk。
- `EmbeddingProvider.identity` 返回 immutable model/tokenizer revision、dimension、实际 device；`async embed(texts)` 保序返回 tuple vectors。索引服务再次验证 count/dimension/finiteness，不信任 provider 单方面自述。
- 正式模型与 tokenizer 均建议固定到 `Qwen/Qwen3-Embedding-0.6B` commit `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`；这来自本轮官方模型 API，并非模型已下载/验证。[官方 metadata](https://huggingface.co/api/models/Qwen/Qwen3-Embedding-0.6B)
- 生产 baseline 使用完整 1024 维，不自动裁剪维度；模型配置上限为 32768 positions。使用官方 AutoModel/AutoTokenizer、last-token pooling、L2 normalization 的文档编码路径，不添加 query instruction/chat template，不照抄示例里的 truncation。[模型说明](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[固定 revision config](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/raw/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/config.json)
- 内部 `EmbeddingExecutionProfile` 初始 `batch_size=1`、`max_input_tokens=8192`、单 provider semaphore 容量 1；token 限制含 special tokens，超过则明确 EMBEDDING 输入限制失败。该较低部署上限不是声称模型只有 8192 context，也不是允许截断。优化 batch/token budget 必须显式配置和验证，不自动 OOM 降级。
- 显式 CUDA 或 CPU；CUDA 不可用不得落 CPU。CUDA 使用固定 float16 / SDPA 路径，CPU 使用明确 float32 路径；记录执行精度与 device，不改变 Stage 1 已冻结的 device-excluded fingerprint。后续改变 pooling/normalization/text 行为必须更新绑定的语义版本。
- provider 在 application composition 中显式初始化并跨调用共享，模型 SDK lazy import；不按 document 重新加载。取消时等待已提交的不可取消推理/SDK工作收束，再释放资源与文档 lease。

### 3.3 Milvus 兼容性扩展，而非整文档累积向量

当前 `replace_revision_set(Sequence[MilvusChunkRecord])` 会构造整个 payload list；不能把它逐 batch 调用，否则每次会删除前一 batch。

保留此 Stage 2 方法与测试原样；新增以下 technical capabilities：

```text
insert_revision_batch(records, *, document_id, revision_id)
wait_revision_visible(document_id, revision_id)
iter_revision_projection(document_id, revision_id, *, batch_size)
```

复用已有 `delete_revision(doc, rev)` 的 scoped delete/barrier。新增 batch insert 校验整个 batch 后才写，拒绝 Parent/跨 scope/坏向量；projection 分页返回无 vector 的 typed row，包含 ID、doc/rev、type、parent、file/source、content、sparse_text、structured metadata，供业务 exact-set 比较。不得增加 schema 字段、重建现有 collection 或暴露 SQL/SDK 给 graph。

保留旧 `_run_sync` 行为；新写入能力在 provider 内只对可确定的服务端数据/schema 拒绝输出 internal `IndexWriteRejectedError`（保持现有安全 error vocabulary，可用 `ProviderResponseError` 子类型）。未知 SDK 异常仍是技术失败；不得靠 traceback 文本猜业务原因。SDK timeout/unavailability 与正常回包的确定性错误必须有分开的测试。

### 3.4 Index → verify → publish 的权威与时序

```text
短 PG 事务：重读 building + elements + chunks，重建并校验 canonical，CHUNKED → INDEXING
    ↓ commit
delete R2 → 确认空集合可见
    ↓
按 canonical order：representation → token check → embed → validate → insert batch
    ↓
flush / strong visibility
    ↓
verify：重新从 PG 生成 expected；读取 Milvus actual，无 re-embedding
    ↓
短 PG 事务：重读 candidate + envelope，校验本次 verification 绑定
    ↓
building triple/slot → current triple/slot；清 building；READY；清 failed_stage
+ query_cache_invalidation_required = true
    ↓ 一个 commit
```

index 服务只接受 scope/context，不接受 caller-supplied chunks。复用 Stage 3 `map_document_from_stored`、`map_chunks_from_stored`、`validate_chunk_set` 验证同 document/revision 的 Parent lineage；不是只查 CHILD IDs。

首次入口要求 CHUNKED。显式重复 index attempt 可接受同一 candidate 的 INDEXING，但必须重新验证持久化 canonical boundary、重新 delete 全部 R2、重写整套；这只是单候选幂等能力，不是扫描/恢复/自动业务 retry。FAILED、current==building、不同 building triple、指纹不匹配全部 fail closed。

verify 比较集合、count、重复、类型与完整 metadata/content；expected 只能来自 PG，不来自 insert 返回值。可达服务上的确定性不一致 → INDEX_VERIFY；读取不可用/timeout → 技术异常。不得把 duplicate 或 Parent 行藏在 set 去重/枚举过滤后。

**验证许可不能是 caller 提供的 `verified=True`。** 每次 runner 创建隔离的 attempt coordinator，verify 产生仅在该 attempt 有效的 internal receipt，绑定 document/revision/content hash/pipeline fingerprint 与 canonical/expected projection digest。receipt 留在显式注入的 attempt 服务实例，不进入 State、不进全局 registry。publish 拒绝缺失、跨调用、过期、已消费 receipt，事务中重读 PG 并重算对应摘要后才写；异常/新 index attempt 使 receipt 失效。

这不是跨 PG/Milvus 的分布式原子提交。保证依赖全程同文档 lease 和 Part I maintenance 单写者边界；不声称可抵抗绕过所有权协议的外部写入。Stage 4 的 READY 只代表单文档发布完成，不代表整个 runtime 可 SERVING。

发布失败必须回滚 triple、parse metadata slots、status、failed_stage 和 marker。既有 R1 的 PG/Milvus 数据在整个 R2 构建期间保持；成功后也不在本 Stage 清旧 R1，交给 Stage 5 reconciliation/global serving safety。

### 3.5 Graph、失败与锁

固定八节点：`prepare_candidate → parse_source → persist_parsed → build_chunks → persist_chunked → index_candidate → verify_candidate → publish_revision`。只有 mutation/discard 与 classified failure/record_failure 两类业务旁路；意外异常直接向上传播。

`BuildRequest` 只携带 SourceFile、预期 pipeline fingerprint；`BuildResult` 只携带 doc/revision、READY/FAILED/SOURCE_MUTATED、安全 stage/reason code。`DocumentBuildState` 可临时携带 immutable context/document/chunks；没有 RuntimeConfig、provider、connection、lease、receipt、vector、RetrievalRecord、异常对象或日志。

runner 先从相对路径导出 document_id，再 acquire lease，**获得锁后** hash/source guard 和创建 candidate；等待期间源可能变化，不能使用入队前 hash。使用现有 `try_acquire` 的可取消等待适配，锁争用不是业务 FAILED。不同文档无需全局锁，模型 semaphore 与文档 lease 分离；连接池必须为 lease 和短 UoW 分别预留容量。

PARSE/CHUNK 复用既有失败记录服务；四种 index failure 新增独立 recorder，只从匹配 candidate 的 INDEXING 转 FAILED。配置/身份/canonical 损坏、依赖不可用、timeout、OOM、取消、程序错误不走该 recorder。解析后 mutation 丢弃 building，已有 current 回 READY，否则 RECEIVED，禁止继续 index。

所有退出路径 finally 释放一次 lease；已发出的同步 SDK 工作 settle 后再解锁，避免后台旧写入污染下一次 attempt。PostgreSQL session 丢失是技术失败，不能把本地 lease 对象仍存在当作锁继续有效；相应故障测试必须覆盖 publish 前的数据库失联。

T8 实施细化：每 invocation 独立 pool adapter 通过既有 lock manager 的公共构造依赖捕获实际持锁连接；复用该连接上的既有 RuntimeMetadataRepository.get() 并及时 rollback 只读事务，在操作边界（尤其 publish 前）证明同一 session 仍可往返。SQL 不进 application/graph，不读取 lease 私有字段，不改冻结 API。观察到失联即 latch technical failure、使 receipt 失效，原 lease 仍是唯一 release owner；probe/release 不并发。真实回归须只终止 lease backend、同时证明普通 UoW 可用。这是 checkpoint 检测，不是 fencing，不能声称消除 probe 后断线或已发出的外部工作窗口。

## 4. 文件职责地图

下列路径均相对仓库根；新 regular package 包含仅 docstring 的 `__init__.py`，不扩展 Stage 1 public export surface。

| 新增路径 | 单一职责 |
| --- | --- |
| `src/atlasrag/domain/indexing/models.py` | Representation/Profile、typed metadata、RetrievalRecord、EmbeddingModelIdentity、PublishResult |
| `src/atlasrag/domain/indexing/failures.py` | internal classified failure stage/reason，不修改 public ErrorCode |
| `src/atlasrag/domain/ingestion/failures.py` | 经 C 批准后新增精确 ChunkInputLimitError |
| `src/atlasrag/providers/embedding/base.py` | EmbeddingProvider Protocol 与输出形状验证边界 |
| `src/atlasrag/providers/embedding/qwen.py` | lazy SDK、固定资产身份、token preflight、受控推理与取消 |
| `src/atlasrag/providers/embedding/profile.py` | immutable execution profile，batch/token/device policy |
| `src/atlasrag/providers/index/projection.py` | 新增 typed read projection 和新 capability 的安全错误类型 |
| `src/atlasrag/services/indexing/ports.py` | indexing 所需 UoW/index/embedding 的窄 Protocol |
| `src/atlasrag/services/indexing/canonical.py` | 从 PG reconstruction + persisted candidate validation |
| `src/atlasrag/services/indexing/representation.py` | 纯确定性文本与 metadata construction |
| `src/atlasrag/services/indexing/records.py` | canonical → RetrievalRecord → MilvusChunkRecord 显式映射 |
| `src/atlasrag/services/indexing/candidate.py` | INDEXING transition、delete/stream batches/barrier |
| `src/atlasrag/services/indexing/verification.py` | 独立 expected/actual exact-set 校验与 receipt |
| `src/atlasrag/services/indexing/publication.py` | receipt binding、单 UoW publish 与 marker |
| `src/atlasrag/services/indexing/failure.py` | 四类 classified index failure 持久化 |
| `src/atlasrag/services/indexing/build.py` | per-invocation coordinator、Stage 3 节点适配，不承载 SQL/SDK |
| `src/atlasrag/graphs/indexing/contracts.py` | internal BuildRequest / BuildResult |
| `src/atlasrag/graphs/indexing/state.py` | 最小 State/Input/Output TypedDict |
| `src/atlasrag/graphs/indexing/document_build.py` | 八个 thin nodes、两种 conditional side routes |
| `src/atlasrag/application/document_build.py` | composition factory + whole-build lease runner |
| `tests/stage4/` | 本阶段离线合同、服务、graph、scope 测试及独立 fakes |
| `tests/integration/test_stage4_{index,publish,locks,build,model_smoke}.py` | 分开的真实存储、锁、E2E、模型验收 |
| `scripts/stage4_verify.sh` | 精确 Stage 4 selector、依赖服务健康检查 |
| `docs/stages/stage-04-part-i-index-publish.md` | 实施中持续更新的证据与最终 Stage report |

修改：`providers/index/milvus.py` 只加 technical capabilities；`services/ingestion/chunking.py` 只做 C 中批准的异常类型补充；`pyproject.toml` / `uv.lock`、`Makefile`、`.github/workflows/integration.yml`、`scripts/check_import_boundaries.py` 及对应测试只做 Stage 4 增量。A 中两个旧 scope test 文件须获批准后精确更新。

不修改：migration、Stage 1 public DTO/State/fingerprint、Stage 2 Repository/UoW/schema、Stage 3 mapper/envelope/chunk output。Stage 2 index content/sparse_text admission 仅按已批准 D 做 nonempty 修订；其他必要变更超出上述清单先报告。

## 5. 按依赖顺序的 TDD 实施任务

每个任务遵循：新增有意义的失败测试 → 运行并确认因目标能力缺失而失败 → 最小实现 → focused tests + 相关回归 → 审查 diff → 独立小 commit。下面命令只在独立 Stage 4 worktree 执行。

### T0 — 批准、隔离、基线与高风险依赖预检

- [x] 记录 A/B/C 的用户决定；有拒绝/歧义先修改本计划，不绕过 guard。
- [x] `git fetch origin`，再次验证两条 Stage 3 ancestor；从 origin/main 创建 `stage/04-part-i-index-publish`，worktree `/tmp/AtlasRAG-stage-04-part-i-index-publish`。如同名路径/分支存在先查状态，不覆盖。
- [x] `uv sync --frozen --offline`、`UV_OFFLINE=1 make verify`；fresh baseline 2273 passed / 47 deselected（77.17s）。沙箱线程阻塞诊断和未改代码的权限外重跑见 Stage report。
- [x] 依赖解析预检：正式 direct runtime 仅增加 LangGraph；非默认组增加 torch/transformers，所有版本最终写入完整 uv.lock，不运行时取 latest。分别验证默认、Docling、MinerU、embedding 与完整联合环境。
- [x] Linux/Windows 锁定官方 torch 2.13.0+cu126，保留 macOS 2.14.1；完整锁未移除任何原有 name/version pair，来源/hash 明确。
- [x] 实际 CUDA float16 tensor/matmul/synchronize 与 SDPA 预检通过；无驱动变更或 CPU fallback。真实 Qwen 模型验收仍属于 T2 与最终硬门。
- [x] 精确 dependency manifests、负向测试、Stage report、独立规格和质量审阅通过；最终 full verify 2280 passed / 47 deselected（77.27s）。T0 提交信息 `chore(stage4): establish approved scope and locked dependencies`。

### T1 — Internal contracts 与 representation

文件：domain/indexing、services/indexing/representation.py；测试 `tests/stage4/test_contracts.py`、`test_representation.py`。

- [x] 参数化测试 CHILD 原文完全相等、TABLE renderer/header/row/sheet 完整、Parent 拒绝、immutable nested metadata、未知字段/坏 revision/缺失 parent fail closed。
- [x] `uv run --frozen pytest tests/stage4/test_contracts.py tests/stage4/test_representation.py -q`，新增测试先因包缺失 RED；schema/byte limit/import guard 后续边界均另有 RED。
- [x] 实现明确 schema/version validators 与纯 builder；不 import tokenizer/model/provider SDK。主代理全量离线验证 2372 passed / 47 deselected（77.54s）；质量审查随后发现既有测试未覆盖的跨阶段 whitespace admission 冲突，不能据此标记 T1 完成。
- [x] 同命令 GREEN；容量边界按 UTF-8 bytes、version 漂移拒绝；已批准 whitespace amendment 的跨阶段回归和真实 Milvus Strong projection 两例通过。最终主代理 full verify 2392 passed / 49 deselected（75.56s），规格/质量复审通过。提交信息 `feat(stage4): add typed retrieval representations`。

### T2 — Embedding protocol 与 Qwen provider

文件：providers/embedding；测试 `tests/stage4/test_embedding.py`、`tests/integration/test_stage4_model_smoke.py`。

- [x] 离线 tests 使用测试目录内受控 tokenizer/model adapter，覆盖 lazy import、显式 init、fixed revision、device 无 fallback、输入长度等于/大于限制、无截断调用、padding/pooling 保序。
- [x] `uv run --frozen pytest tests/stage4/test_embedding.py -q` 确认 RED。
- [x] 实现 Protocol、identity checks、full-input token validation、共享 semaphore 与 count/dimension/NaN/Inf 验证；所有 SDK 实现仅在 provider。
- [x] 验证 fake successful malformed output 是受控 EMBEDDING failure，而 dependency/CUDA/timeout/OOM 与未知异常传播；测试 cancel 时 worker 先 settle 后释放 semaphore。
- [x] 同命令 GREEN；主代理正式真实 Qwen smoke 1 passed（21.48s），全量离线 gate 2484 passed / 50 deselected（81.24s），规格与质量审查通过。记录 pin/device/dtype/dimension/batch，不输出 cache path。提交 `feat(stage4): add pinned local Qwen embedding provider`。

### T3 — Milvus batch 与 projection 能力

文件：providers/index/milvus.py、projection.py；测试 `tests/stage4/test_index_provider.py`、`tests/integration/test_stage4_index.py`。

- [x] RED：batch A/B 连续写不会互相删除；Parent、维度/有限性、scope、重复批内 ID 在 SDK 调用前失败；iterator close、强一致读、取消收束、已知拒绝与 outage 分类各自测试。
- [x] `uv run --frozen pytest tests/stage4/test_index_provider.py -q`：初始 34 RED，追加 late-worker-error/cancel 边界 RED 后修复；独立规格审查 47 passed。
- [x] 添加三项能力，复用现有 schema/record validation；projection 不读取全部 dense vectors，不使用 count 代替 ID/metadata 检查。
- [x] 聚焦新/旧回归 220 passed；主代理真实 Milvus 8 passed（206.84s）、全量离线 2531 passed / 51 deselected（80.64s）；规格与质量审查通过。AST 确认旧方法均不变。提交 `feat(stage4): extend Milvus with bounded batch indexing`。

### T4 — 持久化 candidate authority 与 INDEXING 编排

文件：services/indexing/ports.py、canonical.py、records.py、candidate.py；测试 `tests/stage4/test_candidate_indexing.py`。

实施细化：Milvus provider 新增只读 dimension/analyzer 属性供服务验证注入配置；既有 initialize 再核实物理 collection。持久化 typed chunk strategy version 必须匹配当前 RuntimeConfig。均不修改旧方法语义、schema 或 frozen fingerprint projection。

- [x] RED：伪造 caller chunks 无法成为输入；PG lineage/metadata/schema version 损坏拒绝；模型/指纹/collection dimension mismatch 在 candidate index 写入之前拒绝；index 期间没有活跃 PG transaction。
- [x] `uv run --frozen pytest tests/stage4/test_candidate_indexing.py -q`：初始 33 RED；版本/analyzer guard 追加 3 RED；最终 62 个 T4 测试。
- [x] 实现短 tx 状态转换，PG 重建、retrievable filtering、candidate scoped delete/empty visibility、ordered batches、最终 barrier。上一批 vectors/records 的释放由 weakref 测试验证；PG canonical tuple 不等于允许累积 vector tuple。
- [x] GREEN：4/10 部分技术失败保留 INDEXING，重复 attempt 删除 R2 后重写 10 条，无 stale extra，R1 不变；跨状态/候选拒绝。主代理聚焦 171 passed，全量离线 2593 passed / 51 deselected（81.66s）；规格/质量审查通过。提交 `feat(stage4): index durable canonical candidates`。

### T5 — 独立 verification 与 attempt receipt

文件：services/indexing/verification.py；测试 `tests/stage4/test_candidate_verification.py`。

实施细化：T4 index capability 返回其实际使用的 immutable CanonicalCandidate snapshot，由 per-invocation CandidateVerificationAttempt 内部绑定；不是接受 caller digest。attempt 提供 index、verify、consume_verified（供 T7 的现有事务内调用），receipt 保持私有且一次消费；异常/新 index/并发操作使 generation 失效，旧操作不能重新授权。canonical digest 不含正常 CHUNKED→INDEXING 的状态/时间变化，但覆盖源、完整 canonical 与构建身份；不进入 State。

- [x] RED：missing/extra/duplicate/Parent、错误 doc/rev/type/parent/file/source/sheet/content/sparse_text；PG 在 index 后改变；仅 IDs 正确但 metadata 错误；伪造 insert 返回完整 IDs 均不能 PASS。
- [x] `uv run --frozen pytest tests/stage4/test_candidate_verification.py -q`（既有 frozen 环境直接调用：53 passed；完整 make verify 也覆盖）。
- [x] 从 PG 重新建立 expected，分页读取实际 projection，逐字段/digest 和集合验证，产出 invocation-bound receipt。不给异常提供无证据 PASS。
- [x] GREEN：timeout/unavailable 不变为业务 mismatch；不 re-embed、不实现 production retrieval。真实 Milvus 10 tests passed，包含正式 verifier whitespace cases；完整 make verify 2650 passed。提交消息：`feat(stage4): verify candidate exact sets independently`。

### T6 — Classified failure recorder 与 CHUNK 分类

文件：domain/indexing/failures.py、domain/ingestion/failures.py、services/ingestion/chunking.py、services/indexing/failure.py；测试 `tests/stage4/test_failures.py`。

实施细化：复用已有四类 indexing 的精确异常类型，不为满足文件清单增加无用类型。新增 `classify_index_failure(error)` 和 `record_index_failure(factory, context, error, *, clock)`；未分类异常在读取 clock / 创建 UoW 之前原样传播。ChunkInputLimitError 仅用于已有 table header/cell/header-block/row 四处容量失败；已验证 profile 下的 split/source-map/structural-atom 防御性检查仍是普通 ValueError，不改变成功 canonical 输出、版本或 IDs。

- [x] RED：四类 index failure 仅允许匹配的 INDEXING candidate；PARSE/CHUNK 仍走旧 recorder；R1 triple/envelope 不变；record failure 的 PG 错误继续传播。
- [x] `uv run --frozen pytest tests/stage4/test_failures.py tests/stage3/test_chunking.py -q`（既有 frozen 环境直接调用；最终 174 passed；完整 gate 也覆盖）。
- [x] 只对 C 中批准的明确容量失败引入 ValueError 子类；本任务分类/记录只接受精确业务类型，不捕捉普通 ValueError/Exception；Graph route 在 T8 接入。
- [x] GREEN：普通 ValueError、内部 chunk invariant、SDK unavailable、timeout、cancel 绝不记 FAILED；既有 chunk fixtures/output/ID 不变，AST 与四组 pre-change golden hashes 已验证。完整 make verify 2775 passed。提交消息：`feat(stage4): classify controlled build failures`。

### T7 — 单事务 publish 与 marker

文件：services/indexing/publication.py；测试 `tests/stage4/test_publication.py`、`tests/integration/test_stage4_publish.py`。

实施细化：`publish_revision(factory, context, *, verification: CandidateVerificationAttempt, clock)` 返回既有 `PublishResult`。在 indexing ports 中增加仅含既有 runtime metadata capability 的结构 Protocol，不修改冻结 UoW。attempt 新增仅清除 receipt 并推进 generation 的 `invalidate_verified()`；publisher 的 finally 覆盖 factory/open/clock/write/commit 失败，确保失败后必须重新验证，不释放其他操作的 busy guard，也不暴露或创建 proof。

- [x] RED：None→R1、R1→R2；缺失/跨 invocation/stale/已消费 receipt；错误 building triple/slot/status；current==building；marker 原先 true/false 两种情形。
- [x] `uv run --frozen pytest tests/stage4/test_publication.py -q`（既有 frozen 环境直接调用，与 verification 合计 101 passed；完整 gate 也覆盖）。
- [x] 用原 UoW 在同一事务重读/重验 canonical digest、移动 triple/envelope、READY、clear failure、marker=true；只调用一次显式 commit。
- [x] GREEN，并真实 PG 注入“document.put 完成后 marker 写失败”，证明整个事务回滚且 R1/slots/marker 全不变。真实 Stage 2/3/4 PG 联合回归 40 passed，完整离线 gate 2823 passed；成功不删旧 R1、不连 Redis。提交消息：`feat(stage4): atomically publish verified revisions`。

### T8 — Thin graph 与 whole-build runner

文件：graphs/indexing、services/indexing/build.py、application/document_build.py；测试 `tests/stage4/test_document_build.py`、`test_build_lock.py`。

- [x] RED：记录 node 事件并精确断言八节点顺序；六个业务 failure stage 各走 recorder→END；mutation 只 discard→END；技术/程序错误传播；不能绕过 verify 直达 publish。
- [x] `uv run --frozen pytest tests/stage4/test_document_build.py tests/stage4/test_build_lock.py -q`：主代理 63 passed（1.62s）。
- [x] 实现 StateGraph、显式 injected attempt services、无 checkpointer；runner 同文档锁覆盖整个 ainvoke，finally release。实际持锁 session probe 不宣称 fencing。
- [x] GREEN：成功、业务失败、异常、取消均先 settle 再 release 一次；同文档 B 在 A release 后才能开始 prepare；不同文档可并行。清 key/关闭 tracing/禁止网络的 fake-service graph 仍完成。hash/parser/chunk 实际线程双取消、后续 index batch 停止、startup settlement 均覆盖。
- [x] 检查 State 每个字段与 actual update：vectors、records、config、providers、receipt、异常对象均不能写入。独立规格/质量审阅通过；修正精确 whitelist 的绝对/相对路径差异后，主代理全量 2929 passed / 65 deselected（98.53s）。提交信息 `feat(stage4): compose locked document build subgraph`。

### T9 — 真实 PG/Milvus 故障与 E2E

文件：`tests/integration/test_stage4_index.py`、`test_stage4_publish.py`、`test_stage4_locks.py`、`test_stage4_build.py`，测试 fixtures 只位于 tests。

- [x] 使用唯一 doc/revision/test collection/project，正式服务数据不能复用为破坏性测试对象；启动只需 postgres、etcd、minio、milvus。
- [x] TXT/MD→真实 Stage 3 parse/chunk→测试 FakeEmbeddingProvider→真实 Milvus→verify→真实 PG publish，证明 READY/current/exact set，包含 TABLE metadata 路径。
- [x] R1 已存在时逐点注入 representation/embed/write/verify/publish failure，证明 R1 PG/Milvus 与 current metadata 不变；分别证明 source mutation 没有 current/已有 current 的两种恢复。
- [x] 重复 candidate、partial insert retry、delete visibility、metadata tamper、真实事务 rollback、同/不同文档双连接锁和取消释放全部运行。仅终止实际 lease backend，并证明普通 UoW 可用而 publish 被拒绝。
- [x] 主代理完整真实 gate **36 passed（855.01s）**；服务停止后完整离线 gate **2929 passed / 84 deselected（106.88s）**；规格/质量审阅通过。并发用例只将自有 provider 的 SDK timeout 对齐生产默认 30 秒，保留 60 秒外层 watchdog；未改 Stage 2 fixture、生产逻辑或服务限流。无 skip、mock storage 或额外 retry。提交信息 `test(stage4): cover real index publish and lease invariants`。

### T10 — Scope gates、命令与 CI

文件：Makefile、scripts/stage4_verify.sh、integration.yml、check_import_boundaries.py；测试 `tests/stage4/test_stage4_scope.py`、`test_stage4_commands.py`、现有 checker/workflow tests。

- [x] RED：representations 禁 SDK；graph 禁 SQL/Redis/HTTP/model plumbing；production 禁 tests/benchmarks；embedding SDK 限 providers/embedding；禁止 migration、index_graph、Redis publish、State vectors/deps。初始 56 failures；wildcard capability bypass 另有 3 RED 后精确修复。
- [x] 聚焦/旧 tooling 合计 210 passed；独立规格与质量审阅通过，主代理 full gate 3013 passed / 84 deselected（108.88s），新一轮实际 stage4-verify 的默认 gate 3013 passed（111.83s）、focused 631 passed（21.28s）。
- [x] 新 targets：`test-stage4` 只跑 tests/stage4；`test-stage4-integration` 显式枚举四个 storage/lock/E2E 文件；`stage4-model-smoke` 仅模型文件 + 非默认模型组；`stage4-verify`=默认 verify+focused+真实 integration。
- [x] CI 新 Stage 4 Integration job，默认 frozen dependencies + fake embedding、真实 PG/Milvus，无模型下载。沿用冻结 MinIO build 方案，不依赖无法拉取的旧镜像；Actions 全固定 SHA。
- [x] failure 打诊断、always 清唯一 CI ephemeral project/volumes；本地脚本不 down -v。Stage 2/3 selectors 保持不变，新增 Stage 4 测试不能污染旧 targets。真实脚本通过 bounded health/PG probe、36 项真实 storage gate（842.87s）和保留卷的清理，exit 0。
- [x] GREEN；默认 uv sync/make verify 无 parser/model 组、API key、外部网络服务需求。主代理实际 `make stage4-model-smoke` 真实 CUDA 1 passed（23.65s）；最终静态跨模块审阅通过。提交信息 `ci(stage4): add isolated offline and storage gates`。

### T11 — 全量验证、报告、commit/push 与 PR

- [x] 执行并保存真实结果（2026-11-09 全部 PASS）：

```bash
make verify
make stage2-verify
make stage3-verify
make stage3-parser-smoke
make stage4-verify
make stage4-model-smoke
git diff --check
make architecture-check
```

- [x] inner verify 有明确记录地去重；Stage 2、Docling、MinerU、Stage 4 PG/Milvus、Qwen CUDA 证据均已通过。硬门未失败。
- [x] Stage report 记录 base/HEAD、实际 resolved versions 与 wheel source、模型/tokenizer SHA、device/dtype、representation version、batch、mapping、exact-set、INDEXING 分类、publish/marker rollback、lock/cancel、全部测试、批准事项、deferred scope；不记录 secret、本机模型绝对路径、raw text/vector。
- [ ] 扫描 staged files 和 forbidden artifacts；只 stage 本任务明确文件，不纳入根目录用户手册。提交正常 push 到 Stage 4 branch，不 force。
- [ ] 等最终 HEAD 的 CI 与 Stage 2/3/4 Integration 全绿。文档更正形成新 HEAD 也重新确认对应 gate，不能引用前一个 SHA 的绿灯。
- [ ] 按附件开 PR `Stage 4 — Part I: Index & Publish`，提供证据与 deferred scope；**不自动合并 main**。只有 push 成功、最终远端 gates 通过且所有本地硬门通过才标记 COMPLETE。

## 6. 验收断言示例

以下是应落入测试的核心断言，不是已经实现的 API；T1–T10 将各自完整 arrange/fake/fixture 留在对应测试目录，不复用 production 对 tests 的引用。

```python
assert child_rep.dense_text == child.raw_content
assert child_rep.sparse_text == child.raw_content
assert table_rep.dense_text == render_table_slice(table.table_slice)
assert actual_ids == expected_ids
assert len(actual_rows) == len(expected_ids)  # 不允许 set 掩盖重复
assert all(row.chunk_type != "TEXT_PARENT" for row in actual_rows)
assert stored.current_revision_id == candidate.revision_id
assert stored.building_revision_id is None
assert envelope.current.revision_id == candidate.revision_id
assert envelope.building is None
assert runtime_metadata.query_cache_invalidation_required is True
```

rollback 场景断言完整 StoredDocument/slots/marker 前后相等，不仅检查 current_revision_id；技术失败场景断言没有 record_failure 调用和没有 publish 调用；同文档并发测试用 events/barriers 协调，不用时间猜测。

## 7. 覆盖自检与明确不做

| 附件章节 | 计划覆盖 |
| --- | --- |
| §0–5 基线/authority/scope | §1–2、T0 |
| §6–28 representation/model/records | §3.1–3.3、T1–T2、T4 |
| §29–42 indexing/verification/failure | §3.3–3.5、T3–T6 |
| §43–55 publish/cache/envelope | §3.4、T7、T9 |
| §56–89 graph/lock/cancel/retry/rollback | §3.5、T4–T9 |
| §90–113 integration/model/error/privacy/no migration | T0、T2、T9–T11 |
| §114–123 CI/commands/no weakening/boundaries | §2、T10–T11 |
| §124–136 report/DoD/Git/STOP/final questions | T11、§7 |

不做 directory scan、SyncPlan、top-level index_graph、Reconciliation、旧 revision cleanup、missing-source deletion、MaintenanceCoordinator、RuntimeGate、Redis invalidation、全局 serving safety、Part II retrieval/rerank/answer、benchmark、production observability backend、LangGraph DB recovery。

这份计划选择“兼容增补 provider 能力 + 独立 indexing services”。不选择整文档累积 vectors（破坏 bounded batches），不选择重写 Stage 2 storage（违反冻结边界），不选择仅靠 boolean PASS publish（不能证明本次 candidate 已通过独立验证）。

实施停止条件：A/B/C 未解决、基线回归、冻结 canonical invariant 无法无损表达、必须 migration/重建现有 collection、必须更改 public contract、无法维持原子 marker 或整段 lease、必需额外 Redis/Stage 5 能力、模型/驱动不可运行且无用户批准的替代验收、需要覆盖用户数据、push 错误或最终 CI 不绿。报告应给出具体失败证据与所需决定，不偷偷绕过。

当前执行记录见 `docs/stages/stage-04-part-i-index-publish.md`；未完成全部硬门前不得标记 Stage COMPLETE。
