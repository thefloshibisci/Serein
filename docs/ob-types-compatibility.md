# OB 类型承接：本地契约与迁移边界

本功能不重跑历史摘要、记忆分类、叙事整理或旧导入器，不连接实例，不创建定时任务。上线和实际数据导入需要另行验收。

## Feel

- 存储复用 `documents(kind=scene)` 与完整 revisions，语义固定为 `metadata.memory_role=feel`，提供独立 ID、`read_feel`、`find_feelings`、`write_feel`、`edit_feel`。`read_memory(feel:ID)` 也可读取。旧 Scene 引用保持有效。
- 创建必须给真实 Event/Scene 或仍有效的原话 binding；源 ID、类型、版本、hash 固定于创建时。引发感受的来源保存在 `feel_sources`，不复制为证据，不创建 Event/原话，不改变源正文、digested 或 Persona。
- 源版本修改后仍读当时版本；来源归档不影响感受自身状态。源删除、锁定、失去原话绑定或 hash 不一致时，不返回来源摘要，自动浮现关闭；感受自己仍可显式阅读。
- `find_feelings` 是明确标注的字面检索，支持来源和作者日期过滤；不是已实现的独立语义搜索。语义自动召回复用已有混合候选、重排、卡片上限、冷却与交付登记，并始终标注“当时的主观感受，非事实证据”。
- 自动召回传输 ID 继续为 `scene:ID` 以兼容已交付去重，另带 `semantic_kind=feel`；专用显式入口接受 feel ID。感受和源同轮命中时不重复源正文；自动卡最多展开三个短来源摘要，更多来源显式读取。
- 新建默认不自动浮现。历史 `manual_surface` 原值保留，缺值为未审阅；`dont_surface` / `digested` 阻止自动浮现，`pinned` / `protected` 保留但不自动开启。生命周期与开关分开。
- 旧 annotation 不改；旧独立 feel 与 `kind=feel` 年轮均可预览为独立对象。缺源关联进入待补清单，既不丢正文也不猜关联。whisper/日印象/信件不自动改造成 feel。

## Plan

- 独立表 `authored_plans` / `authored_plan_revisions`，不进入普通记忆索引和事实召回。提供 `create_plan`、`read_plan`、`list_plans`、`update_plan`。
- 保留 active/resolved/abandoned、weight、原正文、相关对象、作者时间、新状态变更记录及旧 `legacy_history`。相关 Scene/Event 可反查计划。
- 新建 active；完成、放弃、重新打开均需显式更新、当前版本、操作者和原因。没有后台推断完成的路径。
- 可挂已有 reminder ID；不自动新建提醒。提醒过期、次数耗尽、关闭均不改变计划。完成计划也不擅自变更旧提醒或长期 Scene。

## 本地迁移流程

1. 保留完整 OB 导出和 Serein 数据库备份。先显式升级本地副本到 schema 10；旧 5..9 库仍可只读，读操作不升级。`Store` 写入初始化只增加三个表，不遍历/改写旧记忆。数据库回到旧程序版本需要相应兼容或备份，不把数据撤销等同数据库降级。
2. 准备 JSON 信封 `{namespace, records, identity_map}`。namespace 固定标识来源库；records 必须有稳定旧 ID 与精确 content；identity_map 由已审核旧 ID 映射到真实 `{kind,id,revision,sha256}`，原话引用还需 binding_id。不要按相似关键词自动生成映射。
3. 如导出含 bucket comments，先用 `records_from_buckets` 提取独立 feel、plan、feel 年轮和明确标记的旧自我记录。保留原 parent bucket 全备份；普通评论不提升类型。有 original_feel_id 指向同批独立 feel 时两者进入重复待审，不静默合并。
4. `python scripts/ob_types.py preview --database LOCAL_COPY --input reviewed.json --output preview.json`。输出新增、复用、缺关联、待审数量及精确预览 hash。旧时间缺失不编造；有时间却无时区进入待审。锁定/删除、未知状态、含混标志保留待审。
5. 审阅后，`apply` 还必须提供 `--batch-id`、`--expected-preview-sha256`、`--backup NEW_BACKUP_PATH`。先独占创建完整 SQLite 备份，再在单一事务重核 source 当前状态/版本/hash、写入、保留每条原始信封、记收据。相同 batch 重试返回原收据；源变化或中断不会留下半批对象。
6. `export` 输出本地完整 feel/plan 版本档案、旧自我映射对象及全部保留的原始信封（包括待审和撤销后的记录）；主数据库备份是完整恢复来源。此导出不是旧网页 Markdown 的无损替代恢复器。
7. `undo --batch-id ...` 仅撤销该批创建且未编辑、无新引用的对象。旧文档、旧批注、原始信封和收据保留；不整库恢复。撤销后如需重做，重新预览和新的 batch ID。

CLI 每次要求新的 `--output` 文件路径，不覆盖已有回执。迁移行为只经本地模块/CLI 暴露；MCP 不提供整批导入、撤销或备份操作。HTTP `/api/authored-types` 提供经过认证的单项工具调用，与 MCP 同一实现。旧网页导入按钮仍走原流程，不能用于本次特殊类型迁移。

## 旧自我内容与现有窗影衔接

Serein 已有窗影 `self_view`，无需再造固定自我分类。旧 OB 的自我入口、分段正文、旧标志与未知元数据是历史资料；迁入不等于认定它们就是现在的自我。

在同一 JSON 信封添加 `self_mappings`，按旧 ID 明确选择每条记录的去向，例如：

```json
{
  "namespace": "my-reviewed-ob-export",
  "records": [
    {"id": "self-understanding", "content": "我愿意保持好奇。", "metadata": {"tags": ["自我"]}},
    {"id": "earlier-self", "content": "这是以前写下的自我认识。", "metadata": {"self_anchor": true}}
  ],
  "identity_map": {},
  "self_mappings": {
    "self-understanding": {"target": "scene"},
    "earlier-self": {"target": "historical_shadow"}
  }
}
```

- 不根据正文中的“自我”二字猜类型。缺映射、锁定/删除或时间/标志含混的记录留待审；`apply` 仍保全原始信封，`export.original_records` 可完整取回。正文、空白、分段及 reflection/affect 不清洗、不改写、不调用模型。时间未知保持 `authored_at=null`，技术导入时间单独保存。
- `scene` 是已有普通 Scene：原正文与首版本完整保留，保留旧归档状态；自动浮现关闭，不因 pinned/protected 变收藏或自动打开。旧标志在 `legacy_flags` 和原始信封中留存。它表达旧作者的认识，不补造 Event 或聊天证据。活动 Scene 可在既有换窗页选入、由 `resume` 显式读取，也能通过 `window_shadow_write(scene_ids=[...])` 关联到新窗影；归档项不自动恢复。
- `historical_shadow` 使用已有窗影档案，`continuity_eligible=false`，可通过 `read_memory(identifier="shadow:返回的ID")`、内部 `WindowShadows.read(ID)` 或带 `window_id` 的 `/api/window-shadows` 显式读全文。列举这些档案用 `/api/window-shadows?include_historical=true`。默认醒来页、`resume` 最新窗影和 `window_shadow_read(index=1)` 不选它，即使旧记录带未来日期或实例没有当前窗影也不选；当前介绍与 Persona 不变。
- 历史窗影可选 `scene_refs`，格式为已核对的 `[{"kind":"scene","id":"...","revision":1,"sha256":"..."}]`，只接受真实可读的普通 Scene 版本。预览固定版本/hash并冻结当前状态；预览后来源修改、删除或锁定使应用失败。若要关联本批尚未创建的 Scene，先完成 Scene 的预览/导入，再拿实际返回的 ID、版本和 hash 预览窗影，不猜未来 ID。
- 当前窗口读过历史后，可以主动写新的窗影 `self_view`，按既有 2000 字规则表达现在的认识；旧长文保留在档案，不截断塞进新画像。这一步是新的作者选择，迁移不会自动执行。
- 相同来源库与旧 ID 具有稳定目标身份。变更正文或映射目标要求先审阅，不能静默从 Scene 改成窗影或重复创建两份。新导入共用预览 hash、完整备份、单事务、幂等收据及定向撤销。撤销遇到对象修改、删除、新窗影/计划/感受/叙事/关系引用或换窗选材时拒绝，避免移除已使用的材料；保留原始信封和收据。

## 已知范围

本轮为后端契约、MCP/HTTP、合成数据与本地迁移工具，包含旧自我内容到现有 Scene/历史窗影的显式映射。还没有专用 feel/plan 网页、独立感受语义搜索页面、旧全库导入 UI 接线或信件锁定体裁承接。没有生产部署、真实数据导入、客户端真机或真实模型质量验收。

原 `Narrative.save_body` 同时修复：只改正文保留全部五类旧 excluded 标记，显式重新选中相应来源才撤销该标记。不会重写已有叙事正文。
