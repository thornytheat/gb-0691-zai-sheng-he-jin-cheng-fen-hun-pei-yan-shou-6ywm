# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## 配方版本切换

`GET /api/recipe`：配方目录，含 V1（投料版）与 V2（验收版）的目标区间、版本指纹与状态。

`POST /api/recipe/switch`：把指定炉次切到一个配方版本。字段：
`id`/`run_id`、`version`（V1/V2 或 1/2）、`batches`（数组或逗号分隔，省略=全部炉次）、
`policy`（投料版/验收版，可选）、`expected_revision`（切换点乐观锁）、`idempotency_key`（可选）。
成功返回 `revision`、`changed`（被改判炉次）、`verdict_changed`（结论翻转的炉次）、`idempotent`。
未选炉次、未知炉次、同炉次重复或版本冲突返回422；切换点过期返回409；失败不写库。

`POST /api/recipe/restore`：`id`、`target_revision`（切换点，0=全部回到投料版）、`expected_revision`。
旧版结论保留在 `acceptance_stored` 与历史事件中，恢复后仍可追回。

`GET /api/runs/<id>/recipe`（或 `GET /api/recipe/state?run_id=`）：当前分配、切换历史、每炉次版本、
验收明细、旧结论快照，以及 `recomputed`/`acceptance_changed`（配方定义更新后的重算结果）。

`GET /api/runs/<id>/evidence?batch=炉次`：已验收依据：结论、归一化成分、带原始行号的来料行。

每个炉次在任一时刻只对应一个配方版本；同一炉次改判记为“是”，版本依据为“投料锁定/切换重判/恢复旧版”。
重跑同一幂等键或同一未变化的切换不会重复改判。
