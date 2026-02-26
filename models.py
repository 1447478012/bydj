from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin' 或 'player'
    player_name = db.Column(db.String(80))  # 打手姓名（仅当 role='player' 时有效）
    is_approved = db.Column(db.Boolean, default=False)  # 新增字段，False表示未审核
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)  # 可选，记录注册时间
    income_mode = db.Column(db.String(20), default='percentage')
    tiered_rates = db.Column(db.Text, nullable=True)
    allow_custom_price = db.Column(db.Boolean, default=False)
    phone = db.Column(db.String(20), nullable=True)
    wechat = db.Column(db.String(50), nullable=True)
    preferred_games = db.Column(db.String(200), nullable=True)
    # 打手展示：直播间、环境照、设备照、认证
    live_room_url = db.Column(db.String(500), nullable=True)   # 直播间链接
    environment_photos = db.Column(db.Text, nullable=True)     # JSON 数组，如 ["player/1/env/xx.jpg"]
    equipment_photos = db.Column(db.Text, nullable=True)        # 代练设备照片 JSON 数组
    equipment_desc = db.Column(db.String(500), nullable=True)  # 代练设备文字描述
    is_certified = db.Column(db.Boolean, default=False)         # 打手认证（管理员设置）
    orders = db.relationship('Order', backref='player', lazy=True)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(50), unique=True, nullable=False)
    game = db.Column(db.String(50))
    task_type = db.Column(db.String(50))
    customer_price = db.Column(db.Float)        # 客户价
    player_price = db.Column(db.Float)           # 打手底价
    status = db.Column(db.String(20), default='待分配')  # 待分配、进行中、待验收、已完成
    screenshot = db.Column(db.String(200))       # 截图文件路径
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    payment_status = db.Column(db.String(20), default='未支付')  # 未支付、已支付、退款中
    payment_method = db.Column(db.String(20))  # 微信、支付宝等
    paid_at = db.Column(db.DateTime)
    rating = db.Column(db.Integer)  # 1-5 分
    comment = db.Column(db.Text)    # 评价内容
    points_used = db.Column(db.Integer, default=0)  # 使用的积分
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id'), nullable=True)
    discount_amount = db.Column(db.Float, default=0)  # 抵扣金额(积分+优惠券)
    original_price = db.Column(db.Float, nullable=True)  # 原价（会员折扣前的价格）
    balance_used = db.Column(db.Float, default=0)  # 本次订单使用余额抵扣的金额
    member_discount = db.Column(db.Float, nullable=True)  # 实际应用的折扣率，如0.9
    coupon = db.relationship('Coupon', foreign_keys=[coupon_id])
    is_custom_offer = db.Column(db.Boolean, default=False)  # 是否顾客自定义报价单（价格表没有/不满意，顾客自填需求与报价）
    service_type = db.Column(db.String(20), default='代肝')  # 代肝 / 陪玩
    duration_hours = db.Column(db.Float, nullable=True)  # 陪玩订单可选：时长（小时）

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='未读')  # 未读 / 已读
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    player = db.relationship('User', backref='feedbacks')

# 会员等级规则：total_spent 累计消费金额
# 青铜 < 1000, 白银 < 5000, 黄金 < 10000, 钻石 >= 10000
LEVEL_RULES = [
    (10000, '钻石', 0.90),   # >= 10000, 9折
    (5000, '黄金', 0.95),    # >= 5000, 95折
    (1000, '白银', 0.98),    # >= 1000, 98折
    (0, '青铜', 1.0),        # < 1000, 原价
]


def get_level_and_discount(total_spent):
    """根据累计消费返回等级和折扣系数"""
    total = total_spent or 0
    for threshold, level, rate in LEVEL_RULES:
        if total >= threshold:
            return level, rate
    return '青铜', 1.0


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(50))  # 可选称呼
    password = db.Column(db.String(200), nullable=True)
    points = db.Column(db.Integer, default=0)  # 积分，1积分=0.01元
    level = db.Column(db.String(20), default='普通')  # 会员等级
    total_spent = db.Column(db.Float, default=0)  # 累计消费金额（已完成订单）
    balance = db.Column(db.Float, default=0)  # 可消费余额（购买会员等存入，下单可抵扣）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    orders = db.relationship('Order', backref='customer', lazy=True)


class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    discount_type = db.Column(db.String(10), default='fixed')  # 'fixed' 固定金额 / 'percent' 比例
    discount_value = db.Column(db.Float, nullable=False)  # 固定金额(元) 或 比例(0.1=10%)
    valid_date = db.Column(db.DateTime, nullable=True)  # 有效期至
    min_amount = db.Column(db.Float, default=0)  # 最低消费金额
    used_by = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order = db.relationship('Order', foreign_keys=[order_id])

class Price(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(50), nullable=False)
    task_type = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), default='元/次')  # 单位
    remark = db.Column(db.String(200))
    service_type = db.Column(db.String(20), default='代肝')  # 代肝 / 陪玩


class PlayerPrice(db.Model):
    """打手对每个任务的个性化报价"""
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    game = db.Column(db.String(50), nullable=False)
    task_type = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)  # 打手个人报价
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    service_type = db.Column(db.String(20), default='代肝')  # 代肝 / 陪玩

    player = db.relationship('User', backref='player_prices')
    __table_args__ = (db.UniqueConstraint('player_id', 'game', 'task_type', name='unique_player_price'),)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    order = db.relationship('Order', backref='payments')
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(20))  # 微信/支付宝
    transaction_id = db.Column(db.String(100))  # 第三方支付流水号
    status = db.Column(db.String(20), default='待支付')  # 待支付、成功、失败
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True)
    type = db.Column(db.String(50))  # 支付成功、状态变更、打手留言、新订单等
    content = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    receiver_type = db.Column(db.String(20), nullable=True)  # 'customer' 或 'player'
    receiver_id = db.Column(db.Integer, nullable=True)  # customer.id 或 user.id

    order = db.relationship('Order', foreign_keys=[order_id])


class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(50), nullable=False)  # remove_player, edit_price, reject_user 等
    target_type = db.Column(db.String(30), nullable=True)  # user, order, price 等
    target_id = db.Column(db.Integer, nullable=True)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='logs', foreign_keys=[user_id])


class UserLog(db.Model):
    """记录用户资料修改"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    field = db.Column(db.String(50))
    old_value = db.Column(db.String(200))
    new_value = db.Column(db.String(200))
    ip = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='user_logs', foreign_keys=[user_id])


class MemberPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # 月卡、季卡等
    price = db.Column(db.Float, nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)  # 有效期天数
    discount = db.Column(db.Float, nullable=False)  # 折扣率，如 0.9 表示9折
    description = db.Column(db.Text)
    is_recommended = db.Column(db.Boolean, default=False)  # 是否推荐
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MemberOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    plan_id = db.Column(db.Integer, db.ForeignKey('member_plan.id'))
    amount = db.Column(db.Float, nullable=False)  # 实付金额
    status = db.Column(db.String(20), default='pending')  # pending, paid, expired, cancelled
    payment_method = db.Column(db.String(20))
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='member_orders')
    plan = db.relationship('MemberPlan', backref='orders')


class CustomerMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), unique=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('member_plan.id'))
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='membership')
    plan = db.relationship('MemberPlan')


class GiftProduct(db.Model):
    """虚拟礼物商品（固定价格）"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    icon = db.Column(db.String(30), default='fa-gift')
    description = db.Column(db.String(200))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GiftOrder(db.Model):
    """礼物订单（待支付/已支付）"""
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    gift_product_id = db.Column(db.Integer, db.ForeignKey('gift_product.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')
    message = db.Column(db.Text)
    pay_token = db.Column(db.String(64), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)

    customer = db.relationship('Customer', backref='gift_orders')
    player = db.relationship('User', backref='gift_orders_received')
    gift_product = db.relationship('GiftProduct', backref='orders')


class CustomerGift(db.Model):
    """顾客给打手赠送的礼物（支付成功后写入）"""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    gift_product_id = db.Column(db.Integer, db.ForeignKey('gift_product.id'), nullable=True)
    gift_type = db.Column(db.String(20), nullable=True)  # 兼容旧数据
    amount = db.Column(db.Float, default=0)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='gifts_sent')
    player = db.relationship('User', backref='gifts_received')
    gift_product = db.relationship('GiftProduct', backref='gifts')


class CustomOfferRequest(db.Model):
    """顾客自定义报价意向：打手接单后顾客支付，支付成功后才创建 Order"""
    id = db.Column(db.Integer, primary_key=True)
    request_no = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    game = db.Column(db.String(50), nullable=False)
    task_type = db.Column(db.String(50), nullable=False)
    notes = db.Column(db.Text)
    offered_price = db.Column(db.Float, nullable=False)
    screenshot = db.Column(db.String(200))
    status = db.Column(db.String(20), default='待接单')  # 待接单、已接单、已支付
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True)  # 支付成功后创建的订单
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # 二次元/无平台价：True 时仅推送给擅长该游戏的打手、平台抽成20%、完成后顾客报价×1.2录入平台价
    is_anime_no_display = db.Column(db.Boolean, default=False)

    customer = db.relationship('Customer', backref='custom_offer_requests')
    player = db.relationship('User', backref='custom_offer_claims')
    order = db.relationship('Order', backref='custom_offer_request', foreign_keys=[order_id])


class PendingTaskRequest(db.Model):
    """打手申请新增任务类型：提交后由管理员审核，通过后加入平台价格表并按抽成规则设平台价。"""
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    game = db.Column(db.String(50), nullable=False)
    task_type = db.Column(db.String(100), nullable=False)
    player_price = db.Column(db.Float, nullable=False)  # 打手报价，通过后平台价=抽成换算
    note = db.Column(db.String(200))
    status = db.Column(db.String(20), default='待审核')  # 待审核、已通过、已驳回
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.String(200))

    player = db.relationship('User', backref='pending_task_requests')


class GameNews(db.Model):
    """游戏资讯"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    summary = db.Column(db.String(500))
    content = db.Column(db.Text)
    cover = db.Column(db.String(200))
    game = db.Column(db.String(50))
    is_published = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContactSetting(db.Model):
    """客服联系信息（单行配置，管理端编辑）"""
    id = db.Column(db.Integer, primary_key=True)
    wechat = db.Column(db.String(100), nullable=True)
    qq = db.Column(db.String(50), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    work_time = db.Column(db.String(100), nullable=True)   # 如：9:00-22:00
    extra_note = db.Column(db.String(500), nullable=True)  # 补充说明
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomerServiceMessage(db.Model):
    """顾客客服留言"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    contact_type = db.Column(db.String(20), nullable=False)   # 微信 / QQ / 手机
    contact_value = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    order_no = db.Column(db.String(50), nullable=True)       # 关联订单号（选填）
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    status = db.Column(db.String(20), default='未读')        # 未读 / 已读 / 已回复
    admin_reply = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    replied_at = db.Column(db.DateTime, nullable=True)

    customer = db.relationship('Customer', backref='service_messages')


class Announcement(db.Model):
    """站内公告（顶部滚动，可关闭）"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Faq(db.Model):
    """常见问题"""
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(500), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
