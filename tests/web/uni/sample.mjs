// APP 客流画像 / 舆情 的样例数据（字段按数据平台接口文档 1.18 ~ 1.31；数值仿 UI 稿）
const S = { scenicId: 'PFTSCA01018088', scenicName: '泰宁九龙潭景区', periodGranularity: '2026H1' }
const rows = (level, key, valueKey, list) => list.map(([n, v, tgi, base]) => ({ ...S, metricLevel: level, [key]: n, [valueKey]: String(v), tgi, nationalBaseline: base == null ? undefined : String(base) }))
export const profileRaw = {
  basic: [
    ...rows('性别', 'metricName', 'proportion', [['男', 58.49, 103, 56.8], ['女', 41.51, 96, 43.2]]),
    ...rows('年龄', 'metricName', 'proportion', [['61以上', 1.8, 145], ['0-17', 5.1, 78], ['18-24', 14.9, 71], ['25-30', 13.2, 92], ['31-35', 14.1, 95], ['36-40', 16.1, 112], ['41-45', 8.8, 135], ['46-60', 24.61, 125]]),
    ...rows('人生阶段', 'metricName', 'proportion', [['中学生', 5.1, 45], ['大学生', 6.1, 60], ['上班族', 65.49, 136], ['自由职业', 18.7, 57], ['退休', 5.5, 136]]),
    ...rows('学历', 'metricName', 'proportion', [['本科', 43.16, 123], ['大专', 25.1, 90], ['硕士', 3.2, 180], ['博士', 0.7, 164]]),
    ...rows('有孩一族', 'metricName', 'proportion', [['是', 61.85, 121], ['否', 38.15, 80]]),
  ],
  spending: [
    ...rows('全国消费水平', 'tagValue', 'popularity', [['低', 5.55, 55, 10], ['中', 38.4, 96, 39.98], ['次低', 15.04, 75, 20], ['次高', 22.55, 112, 20.01], ['高', 18.4, 183, 10.01]]),
    ...rows('收入水平', 'tagValue', 'popularity', [['低', 10, 60], ['中', 40, 100], ['高', 25, 170]]),
    ...rows('是否有车', 'tagValue', 'popularity', [['有车', 92.88, 130], ['无车', 7.12, 30]]),
    ...rows('手机价格', 'tagValue', 'popularity', [['1000以下', 5, 50], ['1000-2999', 30, 90], ['3000-4999', 35, 120], ['5000以上', 30, 150]]),
  ],
  trip: [
    ...rows('旅游距离', 'tagValue', 'popularity', [['近途', 24, 80], ['长途', 31.7, 276], ['无出游', 22.61, 36], ['中途', 21.76, 219]]),
    ...rows('出游月份', 'tagValue', 'popularity', [['08月', 37, 125], ['02月', 41, 99], ['03月', 42, 124], ['05月', 52.18, 149], ['01月', 30, 126], ['04月', 48.57, 155]]),
  ],
  visit: [
    ...['购物偏好:综合商场:55.64:160', '景点偏好:国家级景点:49.02:488', '酒店偏好:星级酒店:44.31:246', '美食偏好:小吃快餐:36.62:189', '景点偏好:公园:35.82:212', '美食偏好:冷饮店:26.29:218']
      .map(s => { const [l, n, v, t] = s.split(':'); return { ...S, metricLevel: l, poiName: n, popularity: v, tgi: +t, nationalBaseline: '9' } }),
  ],
  region: [
    ...[['江西省', 45.57], ['广东省', 8.31], ['浙江省', 8.21], ['福建省', 4.68], ['江苏省', 4.65], ['上海市', 3.36], ['湖南省', 2.1], ['北京市', 1.5], ['四川省', 1.2]]
      .map(([n, v]) => ({ ...S, metricLevel: '客源省份', poiName: n, scenicProvince: '江西省', popularity: String(v) })),
    ...[['鹰潭市', 16.51], ['南昌市', 10.18], ['深圳市', 3.2]].map(([n, v]) => ({ ...S, metricLevel: '客源城市', poiName: n, scenicProvince: '', popularity: String(v) })),
    ...[['鹰潭市', 16.51], ['南昌市', 10.18], ['抚州市', 4.6]].map(([n, v]) => ({ ...S, metricLevel: '省内客源市', poiName: n, scenicProvince: '', popularity: String(v) })),
  ],
  source: [{ ...S, metricLevel: '活跃商圈', poiName: '南昌西站', popularity: '3.94', ranking: 1 }, { ...S, metricLevel: '常驻城市', poiName: '江西省-鹰潭市', popularity: '16.5', ranking: 1 }],
  workHome: [{ ...S, metricLevel: '工作地', poiName: '经济大厦', popularity: '0.61', ranking: 1 }, { ...S, metricLevel: '居住地', poiName: '恒大绿洲北区', popularity: '0.4', ranking: 1 }],
  destination: [
    { ...S, metricLevel: '旅游景点', poiName: '龙虎山地质公园', popularity: 5350 }, { ...S, metricLevel: '旅游景点', poiName: '上清古镇', popularity: 5212 },
    { ...S, metricLevel: '基础设施', poiName: '南昌西站', popularity: 5099 }, { ...S, metricLevel: '生活服务:旅行社', poiName: '某旅行社', popularity: 1200 },
    { ...S, metricLevel: '美食', poiName: '天沐温泉餐厅', popularity: 3100 },
  ],
}
const O = { scenicId: 'PFTSCA01018088', scenicName: '泰宁九龙潭景区' }
export const opinionRaw = {
  base: [{ ...O, publishTime: 20260914, positiveRate1d: 13.4969, positiveRate7d: 68.14, neutralRate7d: 19.76, negativeRate7d: 12.1, positiveRate30d: 60, neutralRate30d: 25, negativeRate30d: 15,
    emotionalScore7d: '3.9012', emotionalScore30d: '3.8', commentCount: 269, commentCount7d: '1513', commentCount30d: 7031, commentCount7dDod: -12.8, commentCount1dDod: -39.41 }],
  period: Array.from({ length: 30 }, (_, i) => ({ ...O, publishTime: 20260816 + (i < 16 ? i : i + 70 - 16 + 14 - 70 + 84 - 84), commentCount: 150 + (i % 7) * 20, positiveCount: 100 + i, neutralCount: 30, negativeCount: 20 })),
  dim: ['游玩体验:4.59', '设施环境:3.9', '餐饮购物:2.93', '交通接驳:3.5'].map(s => { const [n, v] = s.split(':'); return { ...O, publishTime: 20260913, dimensionLevel1: n, dimensionL1Score: v, dimensionL1Score7d: v, dimensionL1Score30d: v } }),
  channel: [['抖音', 610, 0.6803, 415, -5.68, 40.32], ['小红书', 549, 0.7086, 389, -11.79, 36.29], ['快手', 219, 0.653, 143, -16.86, 14.47], ['微博', 94, 0.6277, 59, -13.24, 6.21], ['携程', 41, 0.6098, 25, -3.85, 2.71]]
    .map(([n, c, pr, pc, m, share]) => ({ ...O, publishTime: 20260913, channelName: n, commentCnt7d: c, positiveRate7d: String(pr), positive7dCount: pc, positiveCommentMom7d: String(m), commentCnt7dRate: String(share) })),
  words: [['positive', '值得一去', 51.22], ['positive', '风景不错', 29.28], ['negative', '人太多', 53.27], ['negative', '一般般', 32.58]]
    .map(([t, w, r]) => ({ ...O, publishTime: 20260913, emotionType: t, emotionWord: w, emotionRate7d: r, emotionRate30d: r / 2 })),
}
