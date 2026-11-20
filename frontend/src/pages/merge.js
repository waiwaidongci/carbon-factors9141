import {heading} from '../components/common/header.js';import {table} from '../components/common/table.js';import {esc} from '../utils/format.js';
const STATUSES={importing:'导入中（暂不可见）',completed:'已完成',rejected:'整包拒绝',interrupted:'已中断，可续跑'};
export function merge(c,d,extra={}){const origins=extra.origins||{reports:[],deliveries:[]},imports=extra.imports||[],canMerge=extra.role==='admin';
 return heading('LEDGER EXCHANGE / 07','跨站结算库合并','导出已复核报告与已发交付的一致快照；导入保留来源站与原号，新编号指向同站报告，迁入交付不补发。')
 +`<div class="ledger-actions">${canMerge?`<button id="ledger-export">导出本站结算包</button><label class="file-pick">选择其他站点的导入包<input id="ledger-file" type="file" accept="application/json,.json"></label>`:'<p class="empty">仅负责人可导出与导入结算包；其他角色可核对下方编号对照。</p>'}</div>`
 +`<p class="warning">导入前整包校验：格式、指纹、摘要、父报告与同号冲突全部通过才写入。内容冲突或缺父报告整包拒绝；核对通过前记录不可见；中断后重试同一包从已核对处续跑。</p>`
 +'<h2>报告：本站新编号与来源原号</h2>'+table(origins.reports.map(r=>({新编号:r.id,来源站:r.station,原号:r.origin_id,来源:r.imported?'迁入':'本站'})))
 +'<h2>交付：指向的报告</h2>'+table(origins.deliveries.map(x=>({新编号:x.id,来源站:x.station,原号:x.origin_id,报告新编号:x.run_id,报告原号:x.report_station+'#'+x.report_origin_id,来源:x.imported?'迁入':'本站'})))
 +'<h2>导入批次</h2>'+(imports.length?table(imports.map(x=>({批次:x.id,来源站:x.source_station,状态:STATUSES[x.status]||x.status,检查点:`报告 ${x.checkpoint?.reports??0} · 交付 ${x.checkpoint?.deliveries??0}`,新增:`报告 ${x.counts?.reports??0}（沿用 ${x.counts?.reused_reports??0}） · 交付 ${x.counts?.deliveries??0}（沿用 ${x.counts?.reused_deliveries??0}）`,摘要:(x.digest||'').slice(0,12),原因:x.reason||''}))):'<p class="empty">尚无导入批次。</p>')
 +`<div id="ledger-result" class="ledger-result">${extra.result?`<p>${esc(extra.result)}</p>`:''}</div>`}
