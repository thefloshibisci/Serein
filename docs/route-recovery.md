# 归线历史恢复与明确重建

## 先恢复，不猜补 Track

Track 的当前延续卡会随新的窗口继续更新；`scope` 表示最后使用窗口，不能充当历史归属记录。恢复旧批次不会放宽窗口过滤到未来窗口，也不会编造同名 Track 卡。

日间 Router 现在在同一个事务内保存 normalized `routing_result`、归线缓存及 `pipeline_route_provenance`（每条缓存的来源批次和对应 assignment）。结算写入缓存时同样记录来源。原来的 Router 请求、输出与 ordinal 继续保留。

恢复顺序为：本批快照 → 本批 Router job → 缓存来源批次中的已完成 job/快照 → 旧版已有的有界卡片校验。旧版没有 provenance 时，只在同一 source/window scope 内查与本批原话 ID 实际相交的历史批次；最多 64 个来源批次，每个最多 256 个 Router job。达到上限或结果存在歧义会停下，不会任选一个。

历史回放不调用模型。它核对来源契约、原话编号/顺序/内容/角色/时间、当时的 ordinal、完整 assignment 和 bridge 两端；有下游任务时还核对冻结 component、predecessor 和 ownership。来源日间批次可能比当前结算批次大，因此按已完成 job 的输入块回放，不能使用看过未来原话的日终卡片。没有完整可信材料时保持 `needs_repair`，不会把缺失静默当成功。

完成批的压缩快照保留 Router 初始 `tracks`、`recent`、`next_track_ordinal` 和候选筛选 `input_policy`。已知旧版压缩快照缺失这些字段时，从已保存的 Router 请求恢复初始状态，并继续核对后续状态、原话哈希和时间边界；候选 Track 暂时隐藏后重新出现，也必须符合之前的更新。此次旧版兼容只用于已完成的压缩批，未完成批仍保留代码升级审查，已有待修复计划可能需要明确重建一次。

已发布的归线来源（`routed`、`done`、`superseded_input_budget`）若保留完整初始状态和逐请求 `routing_frame`，即使 `runtime_revision` 与当前代码不同，也会按保存的请求逐帧核对；仍要求相同数据契约和 scope，原话、来源版本、前序卡片、ordinal、assignment 及时间范围必须全部通过校验。这不会重新调用旧任务，也不会把旧记录的版本号改成当前值。缺少完整初始状态的旧压缩批仍仅兼容已知版本，未完成任务仍保留升级审查。

`route producer has an incompatible frozen contract` 指向的是归线来源批次，未必是面板上的当前批次。错误现在包含 producer ID、状态、保存的 contract/runtime、当前 contract/runtime，以及保存和预期 scope。排查升级问题时应将它们与当前批次的 ID、`rebuild_of`、contract/runtime 一起核对。明确重建本来就会将新计划冻结到当前 contract/runtime；新计划引用旧 producer 不能证明新计划继承了旧版本号。不要直接改历史记录的版本号或仅追加版本白名单来消除报错。

恢复结果写入当前批次快照。历史恢复只会补齐数据库里真正缺失的 Track 卡，绝不覆盖任何已经存在的全局卡片；因此即使旧卡缺少 `recent_source_message_ids`，也不会被历史状态倒灌。普通当前归线仍使用 `preserve_newer`。已经完成且相容的 Curator/Writer 结果继续复用。没有逐条保存的旧 provenance 不会在初始化时被猜测填充。

## 界面出口

### 已验证的大帧与短结算批

原话在分批结算或保留待办后，当前待结算范围可能比原 Router 帧短。若整个来源 job 的契约、scope、原话哈希、顺序、assignment 和前序状态均通过验证，但帧读到了当前批次末端之后的原话，系统仍不会裁剪或借用它的 Track 正文。

对于还没有冻结 component 或 Curator/Writer job 的新计划，这种明确的范围不匹配现在按缓存不适用处理：使用当前短批自己的原话和归线前状态，重新请求 Router。旧来源 job 不重跑、不改写。来源缺失、内容/哈希变化、assignment 冲突、歧义来源、缺少完整逐请求证明或已有下游计划时，仍保持待修复，不使用此回退。升级前已经挂起的旧计划仍需要一次明确重建。

### 自动结算与晚到原话

新补传或迟到回复可能从已接受 Router 帧的中间开始。只要逐请求来源证明、原话内容、归属账本和时间边界仍完整，系统会使用该帧真正的归线前卡片；若该 Track 在这一帧里才建立，则暂不把它当作更早的候选。既有较早帧可验证时继续使用较早状态。不会裁剪未来正文、把未来卡片换一个较早编号，或自动清空历史。新批的 `track_history_receipts` 记录来源批次、job、涉及原话和选择原因。

若上一批把未完成问句作为 parked 上下文归过线，后来回复使它成为完整问答，白天归线允许按完整的新问答包处理。前提是旧前缀有完整已接受帧证明、新后缀确实晚于原快照到达、source/session 和回复顺序一致、没有已结算或其他待办预留的来源；旧帧中每条归属仍完整，或有独立的已处理/替换证明。`late_reply_route_receipts` 留下这次扩展依据，旧模型任务及输出保留，不重复结算已完成原话。重复调度已发布的新包不再请求模型。

这两类自动处理不适用于丢失的原话、损坏的哈希、被删除且无处理证明的路由行、未接受的新归属或不完整历史。它们仍进入待修复，不会把损坏误当成正常晚到。没有逐请求完整证明的旧帧也不取得上述自动省略权限。

当天 `pipeline_schedule.completed` 只表示此前已经审阅完可用材料，不再阻止之后补传的历史原话或明确恢复的待办。自动调度继续检查当前结算水位，已审阅且未变化的材料由原批次回执去重；当日水位之后的对话仍等待下一结算窗口。

自动任务返回 `blocked`、`needs_repair`、`retry_wait`、`retained` 或缺模型等状态时，后台会记录批次与原因；同一个状态不会每次轮询重复警告。HTTP `/ready` 只代表服务可用，事件处理状态仍应查看流水线接口与日志。

升级前已处于 `needs_repair` 的批次，如果冻结版本确实与当前版本不同，也会补上 `runtime_contract` 提示，并保留原失败结果；不会清空失败次数或迁移旧计划。冻结版本未变化的归线证据问题仍按原原因处理，不一律标为版本变化。把 `needs_repair` 误送到“重试暂停批次”接口时，会明确提示使用重校验或明确重建入口。

“重新校验并继续”先尝试上述历史恢复。无法恢复时，“作废本批计划并重新归线”会再次明确确认：

- 旧批次置为 `superseded_repair`，原冻结输入、job、模型输出与尝试记录全部保留。
- 仅弃用本批涉及且尚未处理的归线缓存；弃用前把缓存和 provenance 原值写入旧批次审计记录。
- 新计划使用新批次/job ID，保留原始消息与边界，不会把旧 Curator/Writer 输出套到新归线上。此次重建跳过失效缓存及其旧历史入口。
- 若历史 Track 载入时无法验证来源帧（例如 `No bounded historical pre-routing Track frame`），此次确认从本批首条原话开始一个新的归线历史段，不带入旧卡片；新计划和旧批审计记录 `recovery_downgrade`。新归线成功发布后，该聊天后续窗口也忽略截断点之前的归线历史，避免下批再次遇到同一缺口。其他聊天的历史不变，旧卡片、原话、job 和 Event 均保留；不会把未来卡片的正文重新挂到早期来源上。
- 不删除或改写已发布的 Event，不把原话标成 processed/skip。已经有结算收据、原话已被处理或原文内容变动时，拒绝重建。
- 保留队列顺序；新 Track 编号避开当前卡片和被弃用缓存引用的编号。重复提交旧批次 ID 会被拒绝。

HTTP：`POST /v1/pipeline/rebuild`，请求包含 `batch_id` 和 `confirm="REBUILD_PIPELINE_BATCH"`。浏览器经现有同源鉴权代理 `/__serein/pipeline/rebuild`，后端 bearer token 不交给浏览器。MCP 使用 `pipeline_rebuild(batch_id, confirm)`。两者共享 pipeline lease；已有 queued/running 任务时返回 busy。重建本身不调用模型，完成后通过“继续整理”启动新计划；界面确认按钮会接着提交继续请求。

## 验证范围

合成回归覆盖跨窗卡片迁移、旧版无 provenance、来源卡片确实缺行、较大日间批次的早期输入块恢复、旧任务复用、源内容/ordinal/bridge 不匹配、歧义来源、事务回滚、确认重建、旧记录保留、鉴权与 lease 冲突。没有连接真实部署或读取真实对话。本改动不加入并发，不改变 Event Writer 的 500 字软预算与 1500 字异常阈值。

升级回放另使用 `180c94a` 的实际代码生成合成数据库：`93eb2d7` 读取其中已发布的归线会重现 incompatible frozen contract；修复后可以核对旧归线，明确重建的新计划使用当前版本，旧 producer 记录保持原样，重建批次及后续窗口均可结算。此验证仍不代表已核对反馈者实例中的具体 producer 记录。

## 与模型失败暂停的区别

needs_repair 表示无法证明旧归线可以安全恢复，需要先校验或明确重建，不自动跳过或猜补。paused_failure 表示文字 job 累计失败三次：点“重试此批次”保留冻结材料和成功结果，只重新开放未完成步骤的预算。其他聊天可继续，同一聊天后续等待恢复；绕行不适用于 needs_repair。

HTTP 使用 `POST /v1/pipeline/retry-batch` 和 `{batch_id}`，随后调用继续接口。与重建共用 pipeline 执行锁，已有 queued/running 任务时返回 busy。

## 同一 Event 的待补写依赖

Curator 提议更新已有待补写材料预留的 Event 时，host 会优先读取本轮目录中可核验的一份前置待办，交给 Curator 再审阅。仍由模型明确选择整份承接；不会仅凭同一 Track 或目录编号自动取得原话所有权。此读取沿用既有一次上下文补读与提示词预算，超限仍保留材料。

没有承接的冲突 Event 在 Writer 前转为保留，其原话及共享桥接来源不被结算。无依赖的独立 Event 可以继续保存，最终保存前仍核验预留，防止审阅之后状态变化。已有冻结任务需通过明确重新切分生成当前版本计划，不直接改写旧 job。
When a read pending material reserves a candidate Event, Curator receives the exact
pending ID and reserved Event IDs. It must explicitly adopt the whole material to
update that Event, or defer the affected new material. Independent Events continue.
