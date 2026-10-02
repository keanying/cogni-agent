// APP 客流画像 / 舆情 视图模型 —— 直接跑 uni 工程里的真模块
import { buildProfileView, visibleProfileTabs, profilePeriods, regionOf } from '/home/claude/work/uni/digital-brain-uni-v1.1.1/src/utils/profileData.js'
import { buildOpinionView, fields } from '/home/claude/work/uni/digital-brain-uni-v1.1.1/src/utils/opinionData.js'
import { profileRaw, opinionRaw } from './sample.mjs'

const fail = (m: string) => { console.error('FAIL', m); process.exit(1) }
const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b)

// ── 客群 ──
const v = buildProfileView(profileRaw)
const k = Object.fromEntries(v.kpis.map((x: any) => [x.key, x]))
if (k.provinces.value !== 9 || k.provinces.sub !== '省外 54.43%') fail('客源省份覆盖 / 省外: ' + JSON.stringify(k.provinces))
if (k.male.value !== 58.49 || k.male.chip !== 'TGI 103' || k.male.sub !== '女性 41.51%') fail('男性占比')
if (k.age.label !== '主力客群 46-60岁') fail('主力客群取占比最高的年龄段: ' + k.age.label)
if (k.kids.sub !== '有车 92.88%') fail('有孩 + 有车')
if (k.highSpend.value !== 183 || k.highSpend.sub !== '次高+高 40.95%') fail('高消费: ' + JSON.stringify(k.highSpend))
if (k.longTrip.value !== 276) fail('长途出游')
const ov = v.tabs.overview.map((b: any) => b.key)
if (!eq(ov, ['region', 'age', 'visitTop', 'consume', 'months'])) fail('总览模块: ' + ov)
const region = v.tabs.overview[0].option.series[0].data
if (region[0].name !== '华东' || region[0].value !== 66.47) fail('大区汇总: ' + JSON.stringify(region))
const age = v.tabs.overview[1].option.xAxis.data
if (age[0] !== '0-17' || age[age.length - 1] !== '61以上') fail('年龄段排序: ' + age)
const consume = v.tabs.overview[3].option.xAxis.data
if (!eq(consume, ['低', '次低', '中', '次高', '高'])) fail('消费水平按等级排序: ' + consume)
const months = v.tabs.overview[4].option.xAxis.data
if (months[0] !== '01月' || months[months.length - 1] !== '08月') fail('月份排序: ' + months)
if (regionOf('内蒙古自治区') !== '华北' || regionOf('新疆维吾尔自治区') !== '西北' || regionOf('火星') !== '其他') fail('regionOf')
// 取不到的整块丢弃：没有 通勤方式 / 酒店 / 餐饮 数据 → 这几块不出现
const sp = v.tabs.spending.map((b: any) => b.key)
if (sp.includes('commute') || sp.includes('hotelLevel') || sp.includes('foodLevel')) fail('无数据的模块要丢弃: ' + sp)
if (!sp.includes('car')) fail('有车')
// 热门到访地：生活服务:旅行社 归为「旅行社」
const dest = v.tabs.destination.find((b: any) => b.key === 'destCats')
if (!dest.cats.includes('旅行社')) fail('类别名清洗: ' + dest.cats)
// 全空 → 没有任何页签
const empty = buildProfileView({})
if (empty.kpis.length || visibleProfileTabs(empty).length) fail('全空时不出页签')
if (!eq(profilePeriods(new Date(2026, 8, 21)).map((x: any) => x.key), ['2026H1', '2025H1'])) fail('周期(下半年)')
if (!eq(profilePeriods(new Date(2026, 2, 1)).map((x: any) => x.key), ['2025H1', '2024H1'])) fail('周期(上半年未过完)')

// ── 舆情 ──
const o = buildOpinionView(opinionRaw, '7d')
const ok = Object.fromEntries(o.kpis.map((x: any) => [x.key, x]))
// 截图样式:标签按语气着色(环比降 = 绿色 ↓),好评率带两位小数,得分三位小数
if (ok.total.value !== 1513 || !eq(ok.total.chips, [{ text: '↓ 12.80%', tone: 'down' }]) || ok.total.sub !== '环比上一周期') fail('总评价数 / 环比: ' + JSON.stringify(ok.total))
if (ok.positive.value !== '68.14%' || ok.positive.chips[0].text !== '中评 19.76%' || ok.positive.sub !== '差评 12.10%') fail('好评率: ' + JSON.stringify(ok.positive))
if (ok.score.value !== '3.901' || ok.score.chips[0].text !== '满分 5') fail('综合得分: ' + JSON.stringify(ok.score))
if (ok.topChannel.value !== '抖音' || ok.topChannel.chips[0].text !== '40.32%' || ok.topChannel.sub !== '610 条') fail('评论量第一平台: ' + JSON.stringify(ok.topChannel))
if (!o.kpis.every((k: any) => k.icon)) fail('每张指标卡右上角有图标')
const tbl = o.blocks.find((b: any) => b.key === 'channelTable')
if (!eq(tbl.rows[0], ['抖音', '68.03%', 610, 415, '-5.68%'])) fail('平台表(好评占比是小数要 ×100,环比已是百分数不换算): ' + JSON.stringify(tbl.rows[0]))
// 线上真实返回(截图):抖音 9 条好评、环比 80 → +80%;携程 0 条、环比 -100 → -100%(不能再 ×100 成 +8000% / -10000%)
{
  const raw2 = { ...opinionRaw, channel: [
    { publishTime: 20260920, channelName: '抖音', commentCnt7d: 76, positiveRate7d: '0.1184', positive7dCount: 9, positiveCommentMom7d: '80.0000', commentCnt7dRate: 26.76 },
    { publishTime: 20260920, channelName: '携程', commentCnt7d: 7, positiveRate7d: '0.0000', positive7dCount: 0, positiveCommentMom7d: '-100.0000', commentCnt7dRate: 2.46 },
  ] }
  const t2 = buildOpinionView(raw2, '7d').blocks.find((b: any) => b.key === 'channelTable')
  if (!eq(t2.rows.map((r: any) => r[4]), ['+80.00%', '-100.00%'])) fail('好评环比不能 ×100: ' + JSON.stringify(t2.rows))
}
const keys = o.blocks.map((b: any) => b.key)
if (!eq(keys, ['daily', 'emotion', 'score', 'dims', 'trend', 'share', 'channelTable', 'words-positive', 'words-negative'])) fail('舆情模块: ' + keys)
if (o.asOf !== '2026.09.14') fail('数据截至')
// 图形与截图一致:情绪 / 平台是带中心数字的环形,得分是指针仪表,维度是雷达,词是渐变排名条
const blk = (k: string) => o.blocks.find((b: any) => b.key === k)
if (blk('daily').title !== '舆情评价总览' || !blk('daily').option.series[0].areaStyle) fail('舆情评价总览 = 渐变面积')
if (blk('emotion').option.title.text !== '68.14%' || blk('emotion').option.title.subtext !== '好评率') fail('情绪占比中心数字')
if (!eq(blk('emotion').option.color, ['#1989fa', '#ffb547', '#ff6b6b'])) fail('好评蓝 / 中评橙 / 差评红')
if (!blk('score').option.series[0].pointer || blk('score').option.series[0].detail.formatter(3.6081) !== '3.608') fail('得分指针 + 三位小数')
if (blk('dims').option.series[0].type !== 'radar' || blk('dims').option.series[0].label.formatter({ value: 3.70691 }) !== '3.7069' || blk('dims').option.series[0].label.formatter({ value: 3.581 }) !== '3.581' || blk('dims').option.radar.indicator[0].min !== 0) fail('维度雷达 + 四位小数')
if (!eq(blk('trend').option.series.map((x: any) => x.name), ['好评数', '中评数', '差评数'])) fail('情绪趋势三条线')
if (blk('share').option.title.subtext !== '抖音占比') fail('平台占比中心 = 第一平台')
if (blk('words-negative').option.series[0].itemStyle.color.colorStops[1].color !== '#e8503a') fail('负向词红色渐变')
// 近1日：字段不带后缀
const o1 = buildOpinionView(opinionRaw, '1d')
if (o1.kpis.find((x: any) => x.key === 'total').value !== 269) fail('近1日 commentCount 不带后缀')
if (fields.dimScore({ dimensionL1Score: '3.7' }, '1d') !== 3.7) fail('近1日维度分')
// 近1日好评率有、中差评没有 → 情绪占比只画有的
const e1 = o1.blocks.find((b: any) => b.key === 'emotion')
if (e1.option.series[0].data.length !== 1) fail('取不到的扇区丢弃')
if (buildOpinionView({}, '7d').blocks.length || buildOpinionView({}, '7d').kpis.length) fail('全空')
console.log('OK')
