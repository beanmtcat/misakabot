"""Stable community glossary meanings for the second-stage join quiz.

Questions test vocabulary, not promises about providers, pricing, performance,
free-tier eligibility, or the exact path a network route will take.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GlossaryQuestion:
    prompt: str
    options: tuple[str, ...]
    correct_option: str


def _question(prompt: str, correct: str, *distractors: str) -> GlossaryQuestion:
    # 第二个参数 correct 是标准答案，后面的三个参数是干扰项。
    # Onboarding shuffles the options independently for every challenge.
    return GlossaryQuestion(prompt, (correct, *distractors), correct)


GLOSSARY_QUESTIONS: tuple[GlossaryQuestion, ...] = (
    _question(
        "群友说的“杜甫”通常指什么？",
        "独立服务器", "共享域名", "云硬盘快照", "住宅宽带",
    ),
    _question(
        "群友说“白嫖”通常是什么意思？",
        "免费使用或获取资源", "按年付费购买服务器套餐", "额外购买一份流量包", "向商家提交退款工单",
    ),
    _question(
        "“收个玉米”中的“玉米”通常指什么？",
        "域名", "硬盘", "内存", "带宽",
    ),
    _question(
        "VPS 圈说的“oneman 商家”通常指什么？",
        "主要由一个人经营的服务商", "只出售一台服务器的小商家", "只允许一个用户登录的服务器", "只提供一种操作系统的商家",
    ),
    _question(
        "群友说的“小鸡”通常指什么？",
        "VPS 虚拟服务器", "独立服务器整机柜", "域名注册商账号", "家用无线路由器",
    ),
    _question(
        "“母鸡”与“小鸡”的关系，哪项正确？",
        "母鸡是承载 VPS 的物理宿主机", "母鸡是给 VPS 绑定的独立域名", "母鸡是 VPS 定时生成的备份文件", "母鸡是分配给 VPS 的公网 IP",
    ),
    _question(
        "群友吐槽商家“超兽”，通常是在说什么？",
        "资源售出量超过宿主机合理承载", "商家免费给用户升级硬盘容量", "系统默认开启了自动更新功能", "机房整体更换为更高功率的电源设备",
    ),
    _question(
        "VPS 圈的“传家宝”通常指什么？",
        "稀少抢手、转让常有溢价的老套餐", "只能继承、按规定不能转让的服务器", "出厂自带永久保修服务的硬盘", "所有商家都长期提供的免费套餐",
    ),
    _question(
        "“落地鸡”通常承担什么角色？",
        "作为代理链路的最终出口节点", "负责为网站注册域名的机构", "为整个机柜供电的电源设备", "连接本地电脑使用的外接显示器",
    ),
    _question(
        "“中转鸡”通常承担什么角色？",
        "在连接链路中转发流量", "自动为所有域名续费", "为硬盘分区做全盘加密", "为机柜内服务器提供散热",
    ),
    _question(
        "“NAT 鸡”的常见特点是什么？",
        "共享公网 IPv4，靠端口映射访问", "每台独占一整个 IPv4 网段", "完全离线运行，不需要任何网络", "必然拥有无限端口和无限带宽",
    ),
    _question(
        "VPS 社区中的“MJJ”通常称呼哪类人？",
        "购买或折腾 VPS、服务器的玩家", "只在机房负责供电维护的员工", "专门提供域名仲裁服务的机构", "只生产网线和光模块的厂商",
    ),
    _question(
        "群友把硬盘叫“石头盘 / 钻石盘”，通常在吐槽什么？",
        "磁盘读写或 I/O 性能很低", "磁盘外壳用了特别坚硬的材料", "磁盘的容量一定非常大", "磁盘表面带有珠宝材质的涂层",
    ),
    _question(
        "群里常说的“CF”通常是哪家公司？",
        "Cloudflare", "Oracle Cloud", "Netflix", "DigitalOcean",
    ),
    _question(
        "群友说的“乌龟壳 / 乌龟”通常指哪家云？",
        "Oracle Cloud（甲骨文）", "Alibaba Cloud（阿里云）", "Tencent Cloud（腾讯云）", "Cloudflare",
    ),
    _question(
        "买完服务器后被商家“背刺”，通常指什么？",
        "随后降价或推出更划算的产品", "服务器被商家改了 SSH 端口", "服务器按计划正常重启了一次", "域名解析正常生效可以访问",
    ),
    _question(
        "“套路云”通常是群友对哪家厂商的调侃？",
        "阿里云", "腾讯云", "华为云", "甲骨文云",
    ),
    _question(
        "“良心云 / 凉心云”通常是对哪家厂商的调侃？",
        "腾讯云", "阿里云", "华为云", "京东云",
    ),
    _question(
        "“PT”通常指哪类下载社区？",
        "有准入和分享率要求的私有 Tracker", "无需联网即可使用的本地文件管理器", "只提供公共域名解析服务的网站", "专门用来测试 CPU 性能的跑分软件",
    ),
    _question(
        "群友提醒某个 VPS 套餐可能是“灵车”，是什么意思？",
        "服务持续性或商家可靠性风险较大", "商家书面承诺永久不会涨价", "网络线路必然是最高等级的精品网", "机器一定支持免费升级硬件配置",
    ),
    _question(
        "行业黑话“北岸”通常指什么？",
        "备案", "备份", "北向路由", "带宽扩容",
    ),
    _question(
        "服务器讨论中的“DMCA”主要涉及哪类事项？",
        "数字版权相关的侵权投诉", "服务器内存容量的扩展升级", "域名续费时享受的折扣", "硬盘读写性能的测试报告",
    ),
    _question(
        "群友说“给小鸡 DD 一下”，通常准备做什么？",
        "用系统镜像等方式重装系统", "给机器额外购买一个 IPv4 地址", "修改域名的注册人联系信息", "给机柜升级更大功率的电源",
    ),
    _question(
        "“NF / 奈菲”通常指哪个平台？",
        "Netflix", "YouTube", "Cloudflare", "Oracle Cloud",
    ),
    _question(
        "群友所说的“油管”通常指哪个平台？",
        "YouTube", "Netflix", "Oracle Cloud", "Cloudflare",
    ),
    _question(
        "VPS 推荐帖中的“aff 链接”通常是什么？",
        "可给推荐者带来佣金的推广链接", "商家保证最低延迟的专属线路链接", "进入服务器救援模式的入口", "域名在 DNS 中配置的解析记录",
    ),
    _question(
        "线路讨论中的“晚高峰”通常指什么？",
        "晚间流量集中、网络易拥堵的时段", "服务器每天必定自动重启的时刻", "所有商家统一打折促销的时段", "域名每天停止解析的固定维护时段",
    ),
    _question(
        "线路讨论中的“4837”通常对应什么？",
        "中国联通的骨干网络", "中国电信的骨干网络", "中国移动的骨干网络", "一款服务器 CPU 的型号",
    ),
    _question(
        "“CN2 GIA”通常指什么？",
        "中国电信的一种精品国际线路", "中国联通的一种云硬盘格式", "中国移动的一种视频订阅服务", "一种新推出的域名注册后缀",
    ),
    _question(
        "线路讨论中的“CMI”通常指什么？",
        "中国移动国际", "中国联通国际", "中国电信 163 骨干网", "甲骨文云免费套餐",
    ),
    _question(
        "“9929 / 10099”通常与哪家运营商的网络有关？",
        "中国联通", "中国电信", "中国移动", "Cloudflare",
    ),
    _question(
        "“CN2”通常是哪家运营商的网络？",
        "中国电信", "中国联通", "中国移动", "Oracle Cloud",
    ),
    _question(
        "线路讨论中的“163 骨干网”通常指什么？",
        "中国电信 ChinaNet", "网易 163 邮箱服务", "中国联通 AS4837 骨干网", "Cloudflare 的 CDN 网络",
    ),
)
