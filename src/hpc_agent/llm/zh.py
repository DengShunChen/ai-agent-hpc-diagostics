"""Detect Simplified Chinese in text that must be Traditional Chinese (Taiwan usage).

A curated set of common Simplified-only characters. Characters that are also standard
Traditional characters in Taiwan (for example 于 后 里 准 只 干 系 着 云 余 范 并 内, and
警 硬 划 伙 涂 涌 筑 柜 烟 党 叙 种 万, found after a false rejection of 警告) are
deliberately excluded so that correct Traditional text is never rejected.
"""

from __future__ import annotations

SIMPLIFIED_ONLY = frozenset(
    "这为达写储证败级务认调节点时间应该关联实际显现发问题检网络统态记录议访环数据类历产资简处术区链"
    "连缓层质读错误则视观险来对说们会过还进动经让识论设计终断页个从将业机与执释选择输补测试验"
    "载启闭开门户号码线软盘报单双复杂属东车长马鸟鱼风电气专两严临举义乐习乡书买乱争亏亚亲亿"
    "仅仓仪价众优伟传伤伦伪体侦侧债倾偿儿兑兰兴养决况冻净减凭击创删别剂剧劝办励劲劳势勋区医华"
    "协卖卢卫却厅厉压厌县参变叠叹吓吗员响哑唤团园围国图圆圣场坏块坚坛执扩扫扬扰抚抢护担拟拥拦拨"
    "挂挡挤挥损换摄摆摇敌斋旧晓晕暂杀权条杨极构枪标栈栋树样档桥楼欢欧残毁毕汇汉汤沟没泪泽洁浅济"
    "浏浓涨渐温湾湿满滚滞滤滥灭灯灵灾炉炼烂烦烧热爱牵犹狭独献玛画畅疗盏盐监盖确礼祸离积称稳"
    "穷窃竞笔笼签粮紧纠红约纪纯纲纳纵纷纸纹练组细织绑结绕绘给绝继续维综编缘缩罗罚职聪肃肤肿胀脑"
    "腾荣药获营虑虽装规览觉触计订讨训讯讲许设评诉诊词译诗诚话询详语请诸课谁谈谋谓谢谨谱贝负贡财责"
    "货购贯费赖赚赛赶趋跃践踪轨转轮轻较辅辆辞边迁运远违迟递逻遗邮释针钟钢钱铁银锁键闲闻阅队阶际陆"
    "陈隐难雾静顶项顺须顾领频额飞饭馆驱鸡麦黄齐龙"
)


def simplified_characters(text: str) -> list[str]:
    """Distinct Simplified-only characters in text, in order of appearance."""
    return list(dict.fromkeys(ch for ch in text if ch in SIMPLIFIED_ONLY))
