from __future__ import annotations

import re

from .domain import NormalizedMessage, SuspicionSignals

SALE_RE = re.compile(r"(?:出售|出|一手|批发|低价|接单|联系|私聊|私我|私信我|顶我|代理|优惠|自取)")
DEVICE_PREORDER_RE = re.compile(
    r"(?:(?:苹果|iphone|华为|小米|三星|ipad|macbook|ps[345]|switch).{0,12}"
    r"(?:预订|预定|订购|现货|到货|低价|出售|批发|[一二三四五六七八九十\d]+折)"
    r"|(?:预订|预定|订购|现货|到货|低价|出售|批发|[一二三四五六七八九十\d]+折).{0,12}"
    r"(?:苹果|iphone|华为|小米|三星|ipad|macbook|ps[345]|switch))"
)
PROFILE_SERVICE_PROMOTION_RE = re.compile(
    r"(?:拍照|摄影|约拍|代拍|修图|跟拍|写真|证件照).{0,16}"
    r"(?:寻我(?:简介)?|看我(?:简介)?|详情看简介|联系|私聊|加我|接单|预约|报价)"
)
COMPENSATION_RE = re.compile(
    r"(?:日结|周结|月结|[一二三四五六七八九十百千万两\d]+(?:元|rmb|块)(?:/|每)?(?:天|日|小时|h)|"
    r"(?:一天|每日|每天|每小时)\s*[一二三四五六七八九十百千万两\d]+\s*(?:元|rmb|块))"
)
RECRUITMENT_RE = re.compile(r"(?:兼职|招聘|招人|招募|上班|工作机会|小时工|岗位|副业|宝妈岗)")
HIGH_PAY_RE = re.compile(
    r"(?:一|每)(?:个)?小时[一二三四五六七八九十百千万两\d]+(?:万|千|百)?(?:多|起|以上)?"
)
RECRUITMENT_LURE_RE = re.compile(
    r"(?:来几(?:个|位)|勤快的(?:兄弟|姐妹|人)|跟我(?:好好)?干|来(?:跟我)?干活|带你赚)"
)
REWARD_GUARANTEE_RE = re.compile(r"(?:包提|保提).{0,12}(?:小米|su[\s-]?7|汽车|苹果)")
TRANSACTION_CONTEXT_RE = re.compile(
    r"(?:出售|卖|出|收个?|求购|求收|转让|出租).{0,32}(?:vps|小鸡|服务器|主机|订阅|基础配置|pro|as[0-9])"
    r"|(?:vps|小鸡|服务器|主机|订阅|基础配置|pro|as[0-9]).{0,32}"
    r"(?:出售|卖|出|收个?|求购|求收|转让|出租)"
)
# These patterns only select messages for semantic review.  They do not decide
# whether a message is an advertisement: that distinction belongs to the model.
PAYMENT_SERVICE_CONTEXT_RE = re.compile(
    r"(?:搞定|代付|代充|代开|开通|充值|付款|支付|付费|渠道|接单).{0,32}"
    r"(?:gpt|chatgpt|claude|kiro|订阅|会员|账号|服务|软件|任何|全网)"
    r"|(?:gpt|chatgpt|claude|kiro|订阅|会员|账号|服务|软件).{0,32}"
    r"(?:搞定|代付|代充|代开|开通|充值|付款|支付|付费|渠道|接单)"
)
SEMANTIC_REVIEW_REASON = "潜在交易或代办服务：交由语义审核"
FORCED_FIRST_OBSERVED_REASON = "群内首次可见发言：强制大模型审核"
PROHIBITED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "account_trade": ("微信号", "抖音号", "快手号", "小红书号", "qq号", "月卡", "私人号"),
    "financial_promotion": ("虚拟卡", "信用卡", "vcc", "visa", "空投", "返利", "稳赚"),
    "adult_or_gambling": ("嫖娼", "约炮", "赌场", "博彩", "下注"),
    "service_promotion": ("专线", "住宅ip", "独享带宽", "广告投流", "客服"),
}
CALL_TO_ACTION = ("点击", "进群", "加我", "联系我", "领取", "赚钱", "私信")


def detect_suspicion(message: NormalizedMessage, media_type: str | None = None) -> SuspicionSignals:
    normalized = message.normalized_text
    sender_name = message.normalized_sender_name
    reasons: list[str] = []
    score = 0
    matched_categories = [
        name for name, terms in PROHIBITED_CATEGORIES.items() if any(term in normalized for term in terms)
    ]
    name_categories = [
        name for name, terms in PROHIBITED_CATEGORIES.items() if any(term in sender_name for term in terms)
    ]
    name_has_sales_intent = bool(
        SALE_RE.search(sender_name)
        or DEVICE_PREORDER_RE.search(sender_name)
        or (COMPENSATION_RE.search(sender_name) and RECRUITMENT_RE.search(sender_name))
    )
    name_has_service_promotion = bool(PROFILE_SERVICE_PROMOTION_RE.search(sender_name))
    if matched_categories:
        score += 4
        reasons.append(f"高风险类别：{','.join(matched_categories)}")
    if SALE_RE.search(normalized):
        score += 3
        reasons.append("销售或引流意图")
    if DEVICE_PREORDER_RE.search(normalized):
        score += 4
        reasons.append("数码商品预售或销售意图")
    if name_has_sales_intent and (
        name_categories or DEVICE_PREORDER_RE.search(sender_name) or COMPENSATION_RE.search(sender_name)
    ):
        score += 4
        reasons.append("发送者昵称或用户名含广告意图")
    if name_has_service_promotion:
        score += 4
        reasons.append("发送者昵称或用户名含服务推广及引流词")
    if COMPENSATION_RE.search(normalized):
        score += 3
        reasons.append("报酬或有偿服务信息")
    if RECRUITMENT_RE.search(normalized):
        score += 3
        reasons.append("招募或兼职意图")
    if HIGH_PAY_RE.search(normalized):
        score += 4
        reasons.append("异常高时薪承诺")
    if RECRUITMENT_LURE_RE.search(normalized):
        score += 3
        reasons.append("招工话术或收益引流")
    if REWARD_GUARANTEE_RE.search(normalized):
        score += 4
        reasons.append("以高价值奖品作收益承诺")
    requires_semantic_review = bool(
        TRANSACTION_CONTEXT_RE.search(normalized) or PAYMENT_SERVICE_CONTEXT_RE.search(normalized)
    )
    if requires_semantic_review:
        reasons.append(SEMANTIC_REVIEW_REASON)
    if message.urls or message.mentions:
        score += 2
        reasons.append("外链或联系方式")
    if any(term in normalized for term in CALL_TO_ACTION):
        score += 2
        reasons.append("行动号召")
    if message.had_invisible_chars:
        score += 2
        reasons.append("含隐藏字符")
    if media_type:
        score += 1
        reasons.append(f"媒体待分析：{media_type}")
    # A contact request paired with a compensation offer is a common disguised recruitment/service ad.
    # It is quarantined and delegated to the LLM rather than directly treated as a confirmed ad.
    suspicious = bool(
        matched_categories
        or DEVICE_PREORDER_RE.search(normalized)
        or (name_has_sales_intent and (
            name_categories or DEVICE_PREORDER_RE.search(sender_name) or COMPENSATION_RE.search(sender_name)
        ))
        or name_has_service_promotion
        or (SALE_RE.search(normalized) and (message.urls or message.mentions))
        or (SALE_RE.search(normalized) and COMPENSATION_RE.search(normalized))
        or (COMPENSATION_RE.search(normalized) and RECRUITMENT_RE.search(normalized))
        or (HIGH_PAY_RE.search(normalized) and RECRUITMENT_LURE_RE.search(normalized))
        or (HIGH_PAY_RE.search(normalized) and REWARD_GUARANTEE_RE.search(normalized))
    )
    return SuspicionSignals(
        is_suspicious=suspicious,
        score=score,
        reasons=tuple(reasons),
        requires_semantic_review=requires_semantic_review,
    )
