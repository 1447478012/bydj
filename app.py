import os
import json
import secrets
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, session, send_from_directory
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, User, Order, Notification, Payment, Customer, Price, Feedback, Log, Coupon, UserLog, MemberPlan, MemberOrder, CustomerMember, CustomerGift, GiftProduct, GiftOrder, get_level_and_discount, PlayerPrice, CustomOfferRequest, GameNews, PendingTaskRequest, ContactSetting, CustomerServiceMessage, Announcement, Faq
from forms import LoginForm, OrderForm, FeedbackForm, PlayerEditForm
from datetime import datetime, timedelta
from flask import abort
from sqlalchemy import func

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'your-secret-key-change-this'
_db_url = os.environ.get('DATABASE_URL') or 'sqlite:///db.sqlite3'
if _db_url.startswith('postgres://'):
    _db_url = 'postgresql://' + _db_url[11:]
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER') or 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
SITE_IMAGES_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'site')
os.makedirs(SITE_IMAGES_DIR, exist_ok=True)
UPLOAD_WECHAT_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'wechat')
UPLOAD_ALIPAY_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'alipay')
UPLOAD_PRICE_TABLE_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'price_table')
UPLOAD_BG_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'bg')
for d in (UPLOAD_WECHAT_DIR, UPLOAD_ALIPAY_DIR, UPLOAD_PRICE_TABLE_DIR, UPLOAD_BG_DIR):
    os.makedirs(d, exist_ok=True)
SITE_IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
SITE_IMAGE_KEYS = {
    'bg1': '背景轮播图1',
    'bg2': '背景轮播图2',
    'bg3': '背景轮播图3',
    'wechat_pay': '微信支付码',
    'alipay_pay': '支付宝支付码',
}
# 平台抽成比例（0.2 = 20%）。打手固定底价按此换算为平台价：平台价 = 打手价 / (1 - 抽成)
PLATFORM_COMMISSION_RATE = float(os.environ.get('PLATFORM_COMMISSION_RATE', '0.2'))

# 二次元游戏（顾客找不到价格表时列出，不做平台价展示；仅走“顾客报价→推送给擅长打手→20%平台费→完成后加价20%录入平台价”）
# 已上架到价格表的游戏不要写在这里，保持现有规则不变
ANIME_GAMES_CUSTOMER_OFFER = [
    '崩坏3', '崩坏：星穹铁道', '战双帕弥什', '绝区零', '蔚蓝档案', '明日方舟', '少女前线',
    '碧蓝航线', '深空之眼', '鸣潮', '交错战线', '尘白禁区', '斯露德', '来自星尘',
]
ANIME_CUSTOMER_OFFER_COMMISSION = 0.20  # 平台抽成 20%，打手得 80%
ANIME_PRICE_MARKUP = 1.20  # 完成后录入平台价 = 顾客报价 × 1.2


def player_price_to_platform_price(player_price):
    """打手固定底价 → 平台价（顾客价）。打手不可编辑平台价，仅管理员可写 Price 表。"""
    if player_price is None or player_price <= 0:
        return 0
    rate = max(0.01, min(0.99, PLATFORM_COMMISSION_RATE))
    return round(player_price / (1 - rate), 2)


def platform_price_from_player_request(player_price, player):
    """按该打手当前抽成方式，由打手报价反推平台价（保证打手得 player_price 时平台利润最高）。
    fixed 用全局抽成；percentage 用打手设定比例；tiered 用阶梯最低档 75%。
    """
    if player_price is None or player_price <= 0:
        return 0
    if not player:
        return player_price_to_platform_price(player_price)
    mode = (player.income_mode or 'fixed').strip()
    if mode == 'fixed':
        return player_price_to_platform_price(player_price)
    if mode == 'percentage':
        try:
            data = json.loads(player.tiered_rates) if player.tiered_rates else {}
            rate = data.get('rate', 80)
            rate = max(1, min(100, rate)) / 100
            return round(player_price / rate, 2)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return round(player_price / 0.8, 2)
    if mode == 'tiered':
        return round(player_price / 0.75, 2)
    return player_price_to_platform_price(player_price)


def _normalize_task(s):
    """用于模糊匹配：去空格、统一符号。"""
    if not s:
        return ''
    s = (s or '').strip().replace(' ', '').replace('　', '').replace('－', '-').replace('—', '-')
    return s[:50]


def _find_platform_price_fuzzy(game, task_type):
    """先精确匹配，再同 game 下模糊匹配 task_type（包含/被包含/归一化后相等）。返回 Price 或 None。"""
    if not game or not task_type:
        return None
    exact = Price.query.filter_by(game=game.strip(), task_type=task_type.strip()).first()
    if exact:
        return exact
    parsed_norm = _normalize_task(task_type)
    if not parsed_norm:
        return None
    candidates = Price.query.filter_by(game=game.strip()).all()
    for p in candidates:
        platform_norm = _normalize_task(p.task_type)
        if not platform_norm:
            continue
        if parsed_norm == platform_norm:
            return p
        if parsed_norm in platform_norm or platform_norm in parsed_norm:
            return p
        platform_no_dash = platform_norm.replace('-', '')
        parsed_no_dash = parsed_norm.replace('-', '')
        if parsed_no_dash in platform_no_dash or platform_no_dash in parsed_no_dash:
            return p
    return None


def get_site_image_info(key):
    """返回 (filename, mtime, base_dir)。优先 uploads/site/，其次识别文件夹：微信→uploads/wechat/，支付宝→uploads/alipay/。"""
    if key not in SITE_IMAGE_KEYS:
        return None, None, None
    try:
        for f in os.listdir(SITE_IMAGES_DIR):
            if f.startswith(key + '.'):
                path = os.path.join(SITE_IMAGES_DIR, f)
                if os.path.isfile(path) and os.path.splitext(f)[1].lower() in SITE_IMAGE_EXT:
                    return f, int(os.path.getmtime(path)), SITE_IMAGES_DIR
    except OSError:
        pass
    if key == 'wechat_pay':
        return _latest_image_in_dir(UPLOAD_WECHAT_DIR)
    if key == 'alipay_pay':
        return _latest_image_in_dir(UPLOAD_ALIPAY_DIR)
    return None, None, None


def _latest_image_in_dir(directory):
    """返回目录内最新一张图片的 (filename, mtime, directory)。"""
    try:
        best, best_mtime = None, 0
        for f in os.listdir(directory):
            if os.path.splitext(f)[1].lower() in SITE_IMAGE_EXT:
                path = os.path.join(directory, f)
                if os.path.isfile(path):
                    m = int(os.path.getmtime(path))
                    if m > best_mtime:
                        best_mtime, best = m, f
        if best:
            return best, best_mtime, directory
    except OSError:
        pass
    return None, None, None


def list_price_table_images():
    """返回 uploads/price_table/ 内所有图片文件名列表（按修改时间倒序）。"""
    try:
        files = [(f, int(os.path.getmtime(os.path.join(UPLOAD_PRICE_TABLE_DIR, f)))) for f in os.listdir(UPLOAD_PRICE_TABLE_DIR)
                if os.path.splitext(f)[1].lower() in SITE_IMAGE_EXT and os.path.isfile(os.path.join(UPLOAD_PRICE_TABLE_DIR, f))]
        files.sort(key=lambda x: x[1], reverse=True)
        return [f[0] for f in files]
    except OSError:
        return []


def list_background_images():
    """返回背景轮播图列表（图床）：uploads/bg/ 内图片，按修改时间正序。每项为 {'url': str, 'filename': str}。"""
    try:
        files = []
        for f in os.listdir(UPLOAD_BG_DIR):
            if os.path.splitext(f)[1].lower() in SITE_IMAGE_EXT and os.path.isfile(os.path.join(UPLOAD_BG_DIR, f)):
                mtime = int(os.path.getmtime(os.path.join(UPLOAD_BG_DIR, f)))
                files.append((f, mtime))
        files.sort(key=lambda x: x[1])
        return [{'url': url_for('serve_upload', filename='bg/' + f) + '?v=' + str(m), 'filename': f} for f, m in files]
    except OSError:
        return []


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """提供 uploads 目录下的文件（截图、资讯封面等）"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/site_image/<key>')
def serve_site_image(key):
    """提供管理端设置的站点图片（背景、支付码等）；支持识别 uploads/wechat/、uploads/alipay/ 内图片。"""
    filename, _, base_dir = get_site_image_info(key)
    if not filename or not base_dir:
        return redirect(url_for('static', filename=f'images/{key}.jpg'))
    return send_from_directory(base_dir, filename)


db.init_app(app)
login_manager = LoginManager()


def calculate_player_price(order_customer_price, player, current_month_completed=None):
    if player.income_mode == 'percentage':
        try:
            data = json.loads(player.tiered_rates) if player.tiered_rates else {}
            rate = data.get('rate', 80)
            rate = max(1, min(100, rate))
            return round(order_customer_price * rate / 100, 2)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return round(order_customer_price * 0.8, 2)
    elif player.income_mode == 'tiered':
        if current_month_completed is None:
            first_day = datetime(datetime.utcnow().year, datetime.utcnow().month, 1)
            current_month_completed = Order.query.filter(
                Order.player_id == player.id,
                Order.status == '已完成',
                Order.created_at >= first_day
            ).count()
        if current_month_completed < 10:
            rate = 75
        elif current_month_completed <= 20:
            rate = 80
        else:
            rate = 85
        return round(order_customer_price * rate / 100, 2)
    elif player.income_mode == 'fixed':
        return None
    else:
        return None


def auto_assign_order(order_id):
    """自动分配订单：选择使平台利润（顾客价 - 打手报酬）最高的打手；同利润时优先分配给出勤更少的打手。"""
    order = Order.query.get(order_id)
    if not order or order.player_id is not None or order.status != '待分配':
        return False
    players = User.query.filter_by(role='player', is_approved=True).all()
    if not players:
        return False
    customer_price = order.customer_price or 0
    if customer_price <= 0:
        return False
    # 计算每位打手接单的平台利润 = 顾客价 - 打手报酬
    candidates = []
    for player in players:
        reward = get_player_expected_reward(order, player)
        if reward is None:
            continue
        profit = round(customer_price - reward, 2)
        ongoing = Order.query.filter_by(player_id=player.id, status='进行中').count()
        candidates.append((profit, -ongoing, player.id, reward))
    if not candidates:
        return False
    # 按平台利润降序、进行中数量升序（-ongoing 降序即 ongoing 升序）
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_player_id = candidates[0][2]
    best_reward = candidates[0][3]
    order.player_id = best_player_id
    order.player_price = best_reward
    order.status = '进行中'
    if order.customer_id:
        notification = Notification(
            customer_id=order.customer_id,
            order_id=order.id,
            type='订单分配',
            content=f'您的订单 {order.order_no} 已分配给打手，正在处理',
            receiver_type='customer',
            receiver_id=order.customer_id
        )
        db.session.add(notification)
    db.session.add(Notification(
        order_id=order.id,
        type='新订单',
        content=f'订单 {order.order_no} 已分配给您，请及时处理',
        receiver_type='player',
        receiver_id=best_player_id
    ))
    db.session.commit()
    return True


def get_player_price(player_id, game, task_type, default_price):
    """获取打手对该任务的实际报价，如果没有则返回默认价格"""
    player_price = PlayerPrice.query.filter_by(
        player_id=player_id,
        game=game,
        task_type=task_type
    ).first()
    if player_price:
        return player_price.price
    return default_price


def get_player_expected_reward(order, player):
    """打手接该普通订单的预计报酬（用于可接订单列表展示报价）"""
    if not order or not player:
        return None
    customer_price = order.customer_price or 0
    if customer_price <= 0:
        return 0
    reward = calculate_player_price(customer_price, player)
    if reward is not None:
        return reward
    return get_player_price(player.id, order.game, order.task_type, 0)


login_manager.init_app(app)
login_manager.login_view = 'login'


def on_order_completed(order):
    """订单完成时更新顾客 total_spent 和 level；若为二次元顾客报价单则按报价×1.2录入平台价"""
    if not order.customer_id or order.customer_price is None:
        return
    customer = Customer.query.get(order.customer_id)
    if not customer:
        return
    customer.total_spent = (customer.total_spent or 0) + order.customer_price
    customer.level, _ = get_level_and_discount(customer.total_spent)
    # 二次元/无平台价意向完成后：顾客报价加价20%录入平台价
    req = CustomOfferRequest.query.filter_by(order_id=order.id).first()
    if req and getattr(req, 'is_anime_no_display', False):
        platform_price = round((req.offered_price or order.customer_price) * ANIME_PRICE_MARKUP, 2)
        existing = Price.query.filter_by(game=req.game, task_type=req.task_type).first()
        if existing:
            existing.price = platform_price
        else:
            db.session.add(Price(game=req.game, task_type=req.task_type, price=platform_price))


# ---------- 打手管理 ----------
@app.route('/admin/remove_player/<int:player_id>')
@login_required
def remove_player(player_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    
    player = User.query.get_or_404(player_id)
    if player.id == current_user.id:
        flash('不能开除自己')
        return redirect(url_for('admin_players'))
    
    player_name = player.player_name or player.username
    Order.query.filter_by(player_id=player.id).update({'player_id': None})
    db.session.delete(player)
    log = Log(
        user_id=current_user.id,
        action='remove_player',
        target_type='user',
        target_id=player_id,
        detail=f'开除打手 {player_name}（ID:{player_id}）'
    )
    db.session.add(log)
    db.session.commit()
    flash(f'打手 {player_name} 已被开除')
    return redirect(url_for('admin_players'))

@app.route('/admin/players')
@login_required
def admin_players():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    players = User.query.filter_by(role='player').order_by(User.registered_at.desc()).all()
    return render_template('admin_players.html', players=players)

@app.route('/admin/player/edit/<int:player_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_player(player_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    player = User.query.get_or_404(player_id)
    if player.role != 'player':
        flash('该用户不是打手')
        return redirect(url_for('admin_players'))
    form = PlayerEditForm()
    if form.validate_on_submit():
        player.player_name = form.player_name.data
        player.phone = form.phone.data
        player.wechat = form.wechat.data
        player.income_mode = form.income_mode.data
        player.tiered_rates = form.tiered_rates.data or None
        player.allow_custom_price = form.allow_custom_price.data
        player.is_certified = form.is_certified.data
        db.session.commit()
        flash('打手信息已更新')
        return redirect(url_for('admin_players'))
    if request.method == 'GET':
        form.player_name.data = player.player_name
        form.phone.data = player.phone
        form.wechat.data = player.wechat
        form.income_mode.data = player.income_mode or 'fixed'
        form.tiered_rates.data = player.tiered_rates or ''
        form.allow_custom_price.data = player.allow_custom_price or False
        form.is_certified.data = player.is_certified or False
    return render_template('admin/edit_player.html', form=form, player=player)

@app.route('/admin/player/<int:player_id>/orders')
@login_required
def admin_player_orders(player_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    player = User.query.get_or_404(player_id)
    orders = Order.query.filter_by(player_id=player.id).order_by(Order.created_at.desc()).all()
    return render_template('admin_player_orders.html', player=player, orders=orders)

# ---------- 打手注册与审核 ----------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        player_name = request.form['player_name']
        if User.query.filter_by(username=username).first():
            flash('用户名已存在，请换一个')
            return redirect(url_for('register'))
        new_user = User(
            username=username,
            password=generate_password_hash(password),
            role='player',
            player_name=player_name,
            is_approved=False
        )
        db.session.add(new_user)
        db.session.commit()

        # 自动登录新注册的打手
        login_user(new_user)
        flash('注册成功！请选择你的收益模式。')
        return redirect(url_for('choose_income_mode'))
    return render_template('register.html')


@app.route('/player/choose-income-mode', methods=['GET', 'POST'])
@login_required
def choose_income_mode():
    """显示收益模式选择页面（GET）或处理表单提交（POST）"""
    if current_user.role != 'player':
        return redirect(url_for('index'))
    if request.method == 'POST':
        income_mode = request.form.get('income_mode', 'percentage')
        percentage_rate = request.form.get('percentage_rate')
        current_user.income_mode = income_mode
        if income_mode == 'percentage':
            try:
                rate = float(percentage_rate) if percentage_rate else 80
                rate = max(1, min(100, rate))
                current_user.tiered_rates = json.dumps({'rate': rate})
            except (TypeError, ValueError):
                current_user.tiered_rates = json.dumps({'rate': 80})
        else:
            current_user.tiered_rates = None
        db.session.commit()
        flash('收益模式设置成功！')
        return redirect(url_for('player_dashboard'))
    return render_template('player/choose_income_mode.html')


@app.route('/admin/approve')
@login_required
def admin_approve():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    pending = User.query.filter_by(role='player', is_approved=False).order_by(User.registered_at.desc()).all()
    return render_template('admin_approve.html', pending_users=pending)

@app.route('/admin/approve/<int:user_id>/<action>')
@login_required
def approve_user(user_id, action):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    user = User.query.get_or_404(user_id)
    if action == 'approve':
        user.is_approved = True
        db.session.commit()
        flash(f'用户 {user.username} 已通过审核')
    elif action == 'reject':
        username = user.username
        log = Log(
            user_id=current_user.id,
            action='reject_user',
            target_type='user',
            target_id=user_id,
            detail=f'拒绝并删除用户 {username}（ID:{user_id}）'
        )
        db.session.add(log)
        db.session.delete(user)
        db.session.commit()
        flash(f'用户 {username} 已拒绝并删除')
    return redirect(url_for('admin_approve'))

# ---------- 登录/登出 ----------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/choose-login')
def choose_login():
    """游客点击登录后：选择顾客或打手；已登录则直接进入对应页面"""
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('player_dashboard'))
    if session.get('customer_id'):
        return redirect(url_for('customer_index'))
    return render_template('choose_login.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('player_dashboard'))
        flash('用户名或密码错误')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ---------- 管理员仪表盘 ----------
@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))

    today = datetime.utcnow().date()
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    today_total = Order.query.filter(func.date(Order.created_at) == today).count()
    today_completed = Order.query.filter(
        func.date(Order.created_at) == today,
        Order.status == '已完成'
    ).count()
    pending_orders = Order.query.filter(
        Order.status.in_(['待分配', '进行中', '待验收'])
    ).order_by(Order.created_at.desc()).all()
    today_revenue = db.session.query(
        func.coalesce(func.sum(Order.customer_price), 0)
    ).filter(
        func.date(Order.created_at) == today,
        Order.status == '已完成'
    ).scalar() or 0
    player_ranking = db.session.query(
        User.id, User.player_name, User.username,
        func.count(Order.id).label('completed_count')
    ).join(Order, Order.player_id == User.id).filter(
        Order.status == '已完成',
        Order.created_at >= month_start
    ).group_by(User.id).order_by(func.count(Order.id).desc()).limit(5).all()
    
    order_no = request.args.get('order_no', '')
    game = request.args.get('game', '')
    task_type = request.args.get('task_type', '')
    player_id = request.args.get('player_id', type=int)
    status = request.args.get('status', '')
    
    query = Order.query
    if order_no:
        query = query.filter(Order.order_no.contains(order_no))
    if game:
        query = query.filter(Order.game == game)
    if task_type:
        query = query.filter(Order.task_type == task_type)
    if player_id:
        query = query.filter(Order.player_id == player_id)
    if status:
        query = query.filter(Order.status == status)
    
    orders = query.order_by(Order.created_at.desc()).all()
    players = User.query.filter_by(role='player').all()
    return render_template('admin_dashboard.html',
        orders=orders, players=players,
        today_total=today_total, today_completed=today_completed,
        pending_orders=pending_orders, today_revenue=float(today_revenue),
        player_ranking=player_ranking
    )

@app.route('/add_order', methods=['GET', 'POST'])
@login_required
def add_order():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    form = OrderForm()
    players = User.query.filter_by(role='player').all()
    form.player_id.choices = [(p.id, p.player_name) for p in players]
    if form.validate_on_submit():
        order = Order(
            order_no=form.order_no.data,
            game=form.game.data,
            task_type=form.task_type.data,
            customer_price=form.customer_price.data,
            player_price=form.player_price.data,
            player_id=form.player_id.data,
            notes=form.notes.data
        )
        player = User.query.get(form.player_id.data) if form.player_id.data else None
        if player:
            # 优先使用打手个人报价（PlayerPrice），否则使用表单中管理员填写的底价
            base_price = get_player_price(
                player.id, form.game.data, form.task_type.data,
                form.player_price.data or 0
            )
            order.player_price = base_price
            computed = calculate_player_price(order.customer_price or 0, player)
            if computed is not None:
                order.player_price = computed
        db.session.add(order)
        db.session.flush()
        if order.player_id:
            notification = Notification(
                order_id=order.id,
                type='新订单',
                content=f'您有新的待处理订单，订单号{order.order_no}',
                receiver_type='player',
                receiver_id=order.player_id
            )
            db.session.add(notification)
        db.session.commit()
        auto_assign_order(order.id)
        flash('订单添加成功')
        return redirect(url_for('admin_dashboard'))
    return render_template('add_order.html', form=form)

@app.route('/admin/edit_order/<int:order_id>', methods=['GET', 'POST'])
@login_required
def edit_order(order_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    order = Order.query.get_or_404(order_id)
    if request.method == 'POST':
        old_status = order.status
        new_status = request.form['status']
        if old_status != new_status:
            order.status = new_status
            if order.customer_id:
                notification = Notification(
                    customer_id=order.customer_id,
                    order_id=order.id,
                    type='状态变更',
                    content=f'您的订单 {order.order_no} 状态已更新为：{new_status}',
                    receiver_type='customer',
                    receiver_id=order.customer_id
                )
                db.session.add(notification)
            if order.player_id:
                notification = Notification(
                    order_id=order.id,
                    type='状态变更',
                    content=f'订单 {order.order_no} 状态已更新为：{new_status}',
                    receiver_type='player',
                    receiver_id=order.player_id
                )
                db.session.add(notification)
            if new_status == '已完成' and order.customer_id:
                on_order_completed(order)
        db.session.commit()
        flash('订单已更新')
        return redirect(url_for('admin_dashboard'))
    return render_template('admin/edit_order.html', order=order)

# ---------- 打手面板 ----------
@app.route('/player')
@login_required
def player_dashboard():
    if current_user.role != 'player':
        return redirect(url_for('admin_dashboard'))
    orders = Order.query.filter_by(player_id=current_user.id).order_by(Order.created_at.desc()).all()

    # 过去30天每日收入（按订单完成时间分组，已完成订单）
    start_date = datetime.utcnow() - timedelta(days=30)
    daily_income = db.session.query(
        func.date(Order.created_at).label('date'),
        func.sum(Order.player_price).label('total')
    ).filter(
        Order.player_id == current_user.id,
        Order.status == '已完成',
        Order.created_at >= start_date
    ).group_by(func.date(Order.created_at)).all()

    income_map = {str(row.date): float(row.total or 0) for row in daily_income}
    chart_labels = []
    chart_data = []
    for i in range(29, -1, -1):
        d = (datetime.utcnow() - timedelta(days=i)).date()
        chart_labels.append(d.strftime('%m-%d'))
        chart_data.append(income_map.get(str(d), 0))

    return render_template('player_dashboard.html', orders=orders, chart_labels=chart_labels, chart_data=chart_data)


@app.route('/player/income')
@login_required
def player_income():
    """打手收入统计：按日/周/月筛选，支持导出 CSV"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    period = request.args.get('period', 'month')  # day, week, month
    now = datetime.utcnow()
    if period == 'day':
        start = now - timedelta(days=1)
    elif period == 'week':
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    orders = Order.query.filter(
        Order.player_id == current_user.id,
        Order.status == '已完成',
        Order.created_at >= start
    ).order_by(Order.created_at.desc()).all()
    total_income = sum((o.player_price or 0) for o in orders)
    if request.args.get('export') == 'csv':
        from flask import make_response
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['订单号', '游戏', '任务类型', '收入', '完成时间'])
        for o in orders:
            w.writerow([
                o.order_no or '',
                o.game or '',
                o.task_type or '',
                '%.2f' % (o.player_price or 0),
                (o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '')
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8-sig'
        resp.headers['Content-Disposition'] = 'attachment; filename=income_%s.csv' % now.strftime('%Y%m%d')
        return resp
    return render_template('player/income.html', orders=orders, total_income=total_income, period=period)


@app.route('/player/pending_orders')
@login_required
def player_pending_orders():
    """可接订单：仅展示已支付的待分配订单；另提供顾客报价意向（接单后顾客支付，两种模式可选）"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    if not current_user.is_approved:
        flash('您的账号尚未通过审核，无法接单')
        return redirect(url_for('player_dashboard'))
    # 仅已支付的待分配订单（顾客未支付的不作数、不展示）
    orders = Order.query.filter(
        Order.status == '待分配',
        Order.player_id.is_(None),
        Order.payment_status == '已支付'
    ).order_by(Order.created_at.desc()).all()
    # 为每条普通订单计算当前打手接单的预计报酬（报价）
    orders_with_reward = [(o, get_player_expected_reward(o, current_user)) for o in orders]
    # 顾客报价意向：接单后顾客再支付，支付成功才生成订单（另一种模式）
    all_requests = CustomOfferRequest.query.filter_by(status='待接单').order_by(CustomOfferRequest.created_at.desc()).all()
    # 二次元/无平台价意向：仅推送给擅长该游戏的打手
    def _player_can_see_request(req, player):
        if not getattr(req, 'is_anime_no_display', False):
            return True
        if not player or not player.preferred_games:
            return False
        preferred = [x.strip() for x in (player.preferred_games or '').split(',') if x.strip()]
        return (req.game or '').strip() in preferred
    requests = [r for r in all_requests if _player_can_see_request(r, current_user)]
    return render_template('player/pending_orders.html', orders_with_reward=orders_with_reward, requests=requests)


@app.route('/player/claim_order/<int:order_id>', methods=['POST'])
@login_required
def player_claim_order(order_id):
    """打手接单：将待分配订单认领到自己"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    if not current_user.is_approved:
        flash('您的账号尚未通过审核，无法接单')
        return redirect(url_for('player_pending_orders'))
    order = Order.query.get_or_404(order_id)
    if order.status != '待分配' or order.player_id is not None:
        flash('该订单已被接单或状态已变更')
        return redirect(url_for('player_pending_orders'))
    order.player_id = current_user.id
    reward = calculate_player_price(order.customer_price or 0, current_user)
    order.player_price = reward if reward is not None else 0
    db.session.commit()
    if order.customer_id:
        notification = Notification(
            customer_id=order.customer_id,
            order_id=order.id,
            type='状态变更',
            content=f'您的订单 {order.order_no} 已被打手接单，请尽快完成支付',
            receiver_type='customer',
            receiver_id=order.customer_id
        )
        db.session.add(notification)
        db.session.commit()
    flash(f'已接单：{order.order_no}')
    return redirect(url_for('player_dashboard'))


@app.route('/player/claim_request/<int:request_id>', methods=['POST'])
@login_required
def player_claim_custom_request(request_id):
    """打手接单（顾客报价意向）：接单后顾客支付，支付成功后才创建订单"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    if not current_user.is_approved:
        flash('您的账号尚未通过审核，无法接单')
        return redirect(url_for('player_pending_orders'))
    req = CustomOfferRequest.query.get_or_404(request_id)
    if req.status != '待接单':
        flash('该意向已被接单或已支付')
        return redirect(url_for('player_pending_orders'))
    req.player_id = current_user.id
    req.status = '已接单'
    db.session.commit()
    if req.customer_id:
        db.session.add(Notification(
            customer_id=req.customer_id,
            type='意向已接单',
            content=f'您的报价意向 {req.request_no} 已被打手接单，请尽快完成支付。支付成功后订单将生成。',
            receiver_type='customer',
            receiver_id=req.customer_id
        ))
        db.session.commit()
    flash(f'已接单意向：{req.request_no}，等待顾客支付')
    return redirect(url_for('player_dashboard'))


@app.route('/player/my_prices')
@login_required
def player_my_prices():
    if current_user.role != 'player':
        return redirect(url_for('index'))
    # 获取所有全局价格
    global_prices = Price.query.order_by(Price.game, Price.task_type).all()
    # 获取该打手已有的个人报价
    player_prices = PlayerPrice.query.filter_by(player_id=current_user.id).all()
    # 转换成字典方便模板使用
    price_dict = {f"{p.game}_{p.task_type}": p.price for p in player_prices}
    return render_template('player/my_prices.html',
                           global_prices=global_prices,
                           player_prices=price_dict)


@app.route('/player/price', methods=['POST'])
@login_required
def player_save_price():
    if current_user.role != 'player':
        return jsonify({'success': False, 'error': '无权操作'}), 403
    data = request.get_json()
    game = (data.get('game') or '').strip()
    task_type = (data.get('task_type') or '').strip()
    price = data.get('price')
    if not game or not task_type or price is None:
        return jsonify({'success': False, 'error': '参数不完整'}), 400
    try:
        price = float(price)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '价格必须是数字'}), 400

    # 打手不可新增与现有任务类型相似的：只能为平台已有任务类型填写报价；若与已有类型相似则提示使用已有类型
    exact = Price.query.filter_by(game=game, task_type=task_type).first()
    if exact:
        use_game, use_task = exact.game, exact.task_type
    else:
        fuzzy = _find_platform_price_fuzzy(game, task_type)
        if fuzzy:
            return jsonify({
                'success': False,
                'error': f'请使用已有任务类型「{fuzzy.game} - {fuzzy.task_type}」，不可新增相似类型'
            }), 400
        return jsonify({
            'success': False,
            'error': '该任务类型不在平台价格表中，请从列表中选择已有任务或联系管理员添加'
        }), 400

    player_price = PlayerPrice.query.filter_by(
        player_id=current_user.id,
        game=use_game,
        task_type=use_task
    ).first()
    if player_price:
        player_price.price = price
    else:
        db.session.add(PlayerPrice(
            player_id=current_user.id,
            game=use_game,
            task_type=use_task,
            price=price
        ))
    db.session.commit()
    return jsonify({'success': True})


@app.route('/player/task/new', methods=['GET', 'POST'])
@login_required
def player_task_new():
    """打手申请新增任务类型：提交后管理员审核，通过后加入平台价格表（平台价按抽成规则）。"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        game = (request.form.get('game') or '').strip()[:50]
        task_type = (request.form.get('task_type') or '').strip()[:100]
        note = (request.form.get('note') or '').strip()[:200]
        try:
            player_price = float(request.form.get('player_price') or 0)
        except (TypeError, ValueError):
            player_price = 0
        if not game or not task_type:
            flash('请填写游戏和任务类型')
            return redirect(url_for('player_task_new'))
        if player_price <= 0:
            flash('请填写有效的报价金额')
            return redirect(url_for('player_task_new'))
        # 与现有任务类型相似则不允许新增，引导去「我的报价」
        fuzzy = _find_platform_price_fuzzy(game, task_type)
        if fuzzy:
            flash(f'平台已有相似任务类型「{fuzzy.game} - {fuzzy.task_type}」，请直接在【我的报价】中为该任务填写报价')
            return redirect(url_for('player_my_prices'))
        req = PendingTaskRequest(
            player_id=current_user.id,
            game=game,
            task_type=task_type,
            player_price=player_price,
            note=note or None,
            status='待审核'
        )
        db.session.add(req)
        db.session.commit()
        flash('已提交，等待管理员审核。通过后将加入平台价格表，您可在「我的报价」中查看。')
        return redirect(url_for('player_task_requests'))
    games = [r[0] for r in db.session.query(Price.game).distinct().order_by(Price.game).all()]
    return render_template('player/task_new.html', games=games)


@app.route('/player/task/requests')
@login_required
def player_task_requests():
    """打手查看自己提交的新增任务申请列表。"""
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    requests = PendingTaskRequest.query.filter_by(player_id=current_user.id).order_by(PendingTaskRequest.created_at.desc()).all()
    return render_template('player/task_requests.html', requests=requests)


def _apply_parsed_rows_to_player_quotes(rows):
    """将解析出的 (game, task_type, price, unit) 与平台价格表匹配（含模糊匹配），填入当前打手的报价。返回 (matched_count, total_parsed)。打手只能填自己的报价，平台价由抽成规则生成，打手不可编辑。"""
    if not rows:
        return 0, 0
    matched = 0
    for game, task_type, price, _ in rows:
        platform_price = _find_platform_price_fuzzy(game, task_type)
        if not platform_price:
            continue
        use_game = platform_price.game
        use_task = platform_price.task_type
        pp = PlayerPrice.query.filter_by(
            player_id=current_user.id,
            game=use_game,
            task_type=use_task
        ).first()
        if pp:
            pp.price = price
        else:
            db.session.add(PlayerPrice(
                player_id=current_user.id,
                game=use_game,
                task_type=use_task,
                price=price
            ))
        matched += 1
    return matched, len(rows)


@app.route('/player/price/import-image', methods=['GET', 'POST'])
@login_required
def player_price_import_image():
    """打手：上传价格表图片，识别后与平台任务匹配并填入我的报价。"""
    if current_user.role != 'player':
        return redirect(url_for('index'))
    if request.method == 'POST':
        if 'image' not in request.files:
            flash('请选择图片')
            return redirect(url_for('player_price_import_image'))
        f = request.files['image']
        if not f.filename:
            flash('请选择图片')
            return redirect(url_for('player_price_import_image'))
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext not in SITE_IMAGE_EXT:
            flash('请上传图片（jpg/png/gif/webp）')
            return redirect(url_for('player_price_import_image'))
        import tempfile
        path = os.path.join(tempfile.gettempdir(), f"player_img_{current_user.id}_{int(datetime.utcnow().timestamp())}{ext}")
        f.save(path)
        try:
            rows, err = _parse_image_prices(path)
            if err:
                flash('识别失败：' + err)
                return redirect(url_for('player_price_import_image'))
            if not rows:
                flash('未能识别出有效价格行，请确保图片清晰且包含任务与价格')
                return redirect(url_for('player_price_import_image'))
            matched, total = _apply_parsed_rows_to_player_quotes(rows)
            db.session.commit()
            flash(f'图片识别完成：共 {total} 条，其中 {matched} 条与平台任务匹配，已填入您的报价')
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return redirect(url_for('player_my_prices'))
    return redirect(url_for('player_price_import'))


@app.route('/player/price/import-pdf', methods=['GET', 'POST'])
@login_required
def player_price_import_pdf():
    """打手：上传价格表 PDF，解析后与平台任务匹配并填入我的报价。"""
    if current_user.role != 'player':
        return redirect(url_for('index'))
    if request.method == 'GET':
        return redirect(url_for('player_price_import'))
    if request.method == 'POST':
        if 'pdf' not in request.files:
            flash('请选择 PDF 文件')
            return redirect(url_for('player_price_import_pdf'))
        f = request.files['pdf']
        if not f.filename or not f.filename.lower().endswith('.pdf'):
            flash('请上传 PDF 文件')
            return redirect(url_for('player_price_import_pdf'))
        import tempfile
        path = os.path.join(tempfile.gettempdir(), secure_filename(f.filename) or 'upload.pdf')
        f.save(path)
        try:
            rows, err = _parse_pdf_prices(path)
            if err:
                flash('解析失败：' + err)
                return redirect(url_for('player_price_import_pdf'))
            if not rows:
                flash('未能从 PDF 解析出有效价格行')
                return redirect(url_for('player_price_import_pdf'))
            matched, total = _apply_parsed_rows_to_player_quotes(rows)
            db.session.commit()
            flash(f'PDF 解析完成：共 {total} 条，其中 {matched} 条与平台任务匹配，已填入您的报价')
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return redirect(url_for('player_my_prices'))
    return redirect(url_for('player_price_import'))


@app.route('/player/price/import')
@login_required
def player_price_import():
    """打手：识别填入报价入口页（图片 / PDF 上传）。"""
    if current_user.role != 'player':
        return redirect(url_for('index'))
    return render_template('player/price_import_recognize.html')


@app.route('/update_status/<int:order_id>/<string:status>')
@login_required
def update_status(order_id, status):
    order = Order.query.get_or_404(order_id)
    # 权限检查：只有打手自己或管理员可以改
    if current_user.role == 'player' and order.player_id != current_user.id:
        flash('无权操作')
        return redirect(url_for('player_dashboard'))
    old_status = order.status
    if status in ['进行中', '待验收', '已完成']:
        order.status = status
        if old_status != status:
            if order.customer_id:
                notification = Notification(
                    customer_id=order.customer_id,
                    order_id=order.id,
                    type='状态变更',
                    content=f'您的订单 {order.order_no} 状态已更新为：{status}',
                    receiver_type='customer',
                    receiver_id=order.customer_id
                )
                db.session.add(notification)
            if order.player_id and current_user.role == 'admin':
                notification = Notification(
                    order_id=order.id,
                    type='状态变更',
                    content=f'订单 {order.order_no} 状态已更新为：{status}',
                    receiver_type='player',
                    receiver_id=order.player_id
                )
                db.session.add(notification)
            if status == '已完成' and order.customer_id:
                on_order_completed(order)
        db.session.commit()
        flash('状态已更新')
    return redirect(request.referrer)

@app.route('/upload_screenshot/<int:order_id>', methods=['POST'])
@login_required
def upload_screenshot(order_id):
    order = Order.query.get_or_404(order_id)
    if current_user.role != 'player' or order.player_id != current_user.id:
        flash('无权操作')
        return redirect(url_for('player_dashboard'))
    if 'screenshot' not in request.files:
        flash('没有文件')
        return redirect(request.referrer)
    file = request.files['screenshot']
    if file.filename == '':
        flash('未选择文件')
        return redirect(request.referrer)
    if file:
        filename = secure_filename(f"{order.order_no}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        old_status = order.status
        order.screenshot = filename
        order.status = '待验收'
        if order.customer_id and old_status != '待验收':
            notification = Notification(
                customer_id=order.customer_id,
                order_id=order.id,
                type='状态变更',
                content=f'您的订单 {order.order_no} 状态已更新为：待验收',
                receiver_type='customer',
                receiver_id=order.customer_id
            )
            db.session.add(notification)
        db.session.commit()
        flash('截图上传成功')
    return redirect(url_for('player_dashboard'))

@app.route('/player/profile', methods=['GET', 'POST'])
@login_required
def player_profile():
    if current_user.role != 'player':
        return redirect(url_for('index'))

    if request.method == 'POST':
        # 获取表单数据
        player_name = request.form.get('player_name', '')  # 可能只读，但保留
        phone = request.form.get('phone', '')
        wechat = request.form.get('wechat', '')
        preferred_games = request.form.get('preferred_games', '')
        income_mode = request.form.get('income_mode', 'fixed')
        percentage_rate = request.form.get('percentage_rate')
        tiered_rates_raw = request.form.get('tiered_rates', '')

        # 更新用户字段
        current_user.player_name = player_name or current_user.player_name
        current_user.phone = phone.strip() or None
        current_user.wechat = wechat.strip() or None
        current_user.preferred_games = preferred_games.strip() or None
        current_user.income_mode = income_mode

        # 打手展示：直播间链接、设备描述
        current_user.live_room_url = (request.form.get('live_room_url') or '').strip() or None
        current_user.equipment_desc = (request.form.get('equipment_desc') or '').strip() or None

        # 环境照片、设备照片上传（追加到现有列表）
        def _save_player_photos(field_key, subdir):
            """保存多张图片到 uploads/player/<id>/<subdir>/，返回相对路径列表"""
            base = os.path.join(app.config['UPLOAD_FOLDER'], 'player', str(current_user.id), subdir)
            os.makedirs(base, exist_ok=True)
            existing = []
            try:
                existing = json.loads(getattr(current_user, field_key) or '[]')
            except (TypeError, ValueError):
                pass
            if not isinstance(existing, list):
                existing = []
            files = request.files.getlist(field_key) if request.files else []
            for f in files:
                if not f or not f.filename:
                    continue
                ext = os.path.splitext(secure_filename(f.filename))[1].lower()
                if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                    continue
                name = f"_{int(datetime.utcnow().timestamp())}_{secure_filename(f.filename)}"
                rel = f"player/{current_user.id}/{subdir}/{name}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], rel.replace('/', os.sep)))
                existing.append(rel)
            return existing[:20]  # 最多保留 20 张

        env_photos = _save_player_photos('environment_photos', 'env')
        current_user.environment_photos = json.dumps(env_photos)
        equip_photos = _save_player_photos('equipment_photos', 'equipment')
        current_user.equipment_photos = json.dumps(equip_photos)

        # 处理收益模式相关字段
        if income_mode == 'percentage':
            try:
                rate = float(percentage_rate) if percentage_rate else 80
                current_user.tiered_rates = json.dumps({'rate': rate})
            except (TypeError, ValueError):
                current_user.tiered_rates = json.dumps({'rate': 80})
        elif income_mode == 'tiered':
            current_user.tiered_rates = tiered_rates_raw.strip() or None
        else:
            current_user.tiered_rates = None

        db.session.commit()
        flash('资料更新成功')
        return redirect(url_for('player_profile'))

    # GET：解析比例抽成用于表单回显
    percentage_rate = 80
    if current_user.income_mode == 'percentage' and current_user.tiered_rates:
        try:
            data = json.loads(current_user.tiered_rates)
            percentage_rate = int(float(data.get('rate', 80)))
            percentage_rate = max(1, min(100, percentage_rate))
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass
    env_list = []
    equip_list = []
    try:
        if current_user.environment_photos:
            env_list = json.loads(current_user.environment_photos)
        if not isinstance(env_list, list):
            env_list = []
    except (TypeError, ValueError):
        pass
    try:
        if current_user.equipment_photos:
            equip_list = json.loads(current_user.equipment_photos)
        if not isinstance(equip_list, list):
            equip_list = []
    except (TypeError, ValueError):
        pass
    return render_template('player/profile.html', user=current_user, percentage_rate=percentage_rate, env_list=env_list, equip_list=equip_list)

# ---------- 打手反馈 ----------
@app.route('/player/feedback', methods=['GET', 'POST'])
@login_required
def player_feedback():
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    form = FeedbackForm()
    if form.validate_on_submit():
        feedback = Feedback(
            title=form.title.data,
            content=form.content.data,
            player_id=current_user.id
        )
        db.session.add(feedback)
        db.session.commit()
        flash('反馈已提交，感谢你的建议！')
        return redirect(url_for('player_dashboard'))
    return render_template('player_feedback.html', form=form)

@app.route('/admin/feedback')
@login_required
def admin_feedback():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    feedbacks = Feedback.query.order_by(Feedback.created_at.desc()).all()
    return render_template('admin_feedback.html', feedbacks=feedbacks)

@app.route('/admin/feedback/<int:feedback_id>')
@login_required
def view_feedback(feedback_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    feedback = Feedback.query.get_or_404(feedback_id)
    return render_template('feedback_detail.html', feedback=feedback)

# ---------- 客服模块 ----------
@app.route('/customer/service', methods=['GET', 'POST'])
def customer_service():
    """联系客服：展示联系方式 + 留言表单"""
    contact = ContactSetting.query.first()
    if not contact:
        contact = ContactSetting(wechat='1447478012', qq='1447478012', phone='', work_time='9:00-22:00', extra_note='')
        db.session.add(contact)
        db.session.commit()
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        contact_type = (request.form.get('contact_type') or '微信').strip()
        contact_value = (request.form.get('contact_value') or '').strip()
        content = (request.form.get('content') or '').strip()
        order_no = (request.form.get('order_no') or '').strip() or None
        if not name or not contact_value or not content:
            flash('请填写称呼、联系方式与留言内容')
            return redirect(url_for('customer_service'))
        customer_id = session.get('customer_id')
        msg = CustomerServiceMessage(
            name=name,
            contact_type=contact_type,
            contact_value=contact_value,
            content=content,
            order_no=order_no,
            customer_id=customer_id,
            status='未读'
        )
        db.session.add(msg)
        db.session.commit()
        flash('您的留言已提交，客服会尽快回复。')
        return redirect(url_for('customer_service'))
    return render_template('customer/service.html', contact=contact)


@app.route('/admin/service/contact', methods=['GET', 'POST'])
@login_required
def admin_service_contact():
    """管理端：编辑客服联系信息"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    contact = ContactSetting.query.first()
    if not contact:
        contact = ContactSetting(wechat='1447478012', qq='1447478012', phone='', work_time='9:00-22:00', extra_note='')
        db.session.add(contact)
        db.session.commit()
    if request.method == 'POST':
        contact.wechat = (request.form.get('wechat') or '').strip() or None
        contact.qq = (request.form.get('qq') or '').strip() or None
        contact.phone = (request.form.get('phone') or '').strip() or None
        contact.work_time = (request.form.get('work_time') or '').strip() or None
        contact.extra_note = (request.form.get('extra_note') or '').strip() or None
        db.session.commit()
        flash('客服联系信息已保存')
        return redirect(url_for('admin_service_contact'))
    return render_template('admin/service_contact.html', contact=contact)


@app.route('/admin/service/messages')
@login_required
def admin_service_messages():
    """管理端：顾客客服留言列表"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    messages = CustomerServiceMessage.query.order_by(CustomerServiceMessage.created_at.desc()).all()
    return render_template('admin/service_messages.html', messages=messages)


@app.route('/admin/service/message/<int:msg_id>', methods=['GET', 'POST'])
@login_required
def admin_service_message_reply(msg_id):
    """管理端：查看并回复留言"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    msg = CustomerServiceMessage.query.get_or_404(msg_id)
    if request.method == 'POST':
        reply = (request.form.get('admin_reply') or '').strip()
        msg.admin_reply = reply or None
        msg.status = '已回复' if reply else '已读'
        msg.replied_at = datetime.utcnow() if reply else None
        db.session.commit()
        flash('已保存回复' if reply else '已标记为已读')
        return redirect(url_for('admin_service_messages'))
    if msg.status == '未读':
        msg.status = '已读'
        db.session.commit()
    return render_template('admin/service_message_reply.html', msg=msg)


@app.route('/admin/coupons')
@login_required
def admin_coupons():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()
    return render_template('admin_coupons.html', coupons=coupons, now=datetime.utcnow())

@app.route('/admin/coupon/add', methods=['GET', 'POST'])
@login_required
def admin_coupon_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        discount_type = request.form.get('discount_type', 'fixed')
        discount_value = float(request.form.get('discount_value', 0))
        valid_date_str = request.form.get('valid_date', '')
        min_amount = float(request.form.get('min_amount', 0))
        valid_date = datetime.strptime(valid_date_str, '%Y-%m-%d') if valid_date_str else None
        if Coupon.query.filter_by(code=code).first():
            flash(f'优惠券码 {code} 已存在')
            return redirect(url_for('admin_coupon_add'))
        coupon = Coupon(code=code, discount_type=discount_type, discount_value=discount_value,
                        valid_date=valid_date, min_amount=min_amount)
        db.session.add(coupon)
        db.session.commit()
        flash('优惠券添加成功')
        return redirect(url_for('admin_coupons'))
    return render_template('admin_coupon_form.html', coupon=None)

@app.route('/admin/coupon/edit/<int:coupon_id>', methods=['GET', 'POST'])
@login_required
def admin_coupon_edit(coupon_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    coupon = Coupon.query.get_or_404(coupon_id)
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        discount_type = request.form.get('discount_type', 'fixed')
        discount_value = float(request.form.get('discount_value', 0))
        valid_date_str = request.form.get('valid_date', '')
        min_amount = float(request.form.get('min_amount', 0))
        valid_date = datetime.strptime(valid_date_str, '%Y-%m-%d') if valid_date_str else None
        existing = Coupon.query.filter(Coupon.code == code, Coupon.id != coupon_id).first()
        if existing:
            flash(f'优惠券码 {code} 已存在')
            return redirect(url_for('admin_coupon_edit', coupon_id=coupon_id))
        coupon.code = code
        coupon.discount_type = discount_type
        coupon.discount_value = discount_value
        coupon.valid_date = valid_date
        coupon.min_amount = min_amount
        db.session.commit()
        flash('优惠券更新成功')
        return redirect(url_for('admin_coupons'))
    return render_template('admin_coupon_form.html', coupon=coupon)

@app.route('/admin/coupon/delete/<int:coupon_id>')
@login_required
def admin_coupon_delete(coupon_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    coupon = Coupon.query.get_or_404(coupon_id)
    if coupon.used_by:
        flash('该优惠券已被使用，无法删除')
        return redirect(url_for('admin_coupons'))
    db.session.delete(coupon)
    db.session.commit()
    flash('优惠券已删除')
    return redirect(url_for('admin_coupons'))

@app.route('/admin/logs')
@login_required
def admin_logs():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = Log.query.order_by(Log.created_at.desc()).paginate(page=page, per_page=per_page)
    return render_template('admin_logs.html', pagination=pagination)

@app.route('/admin/feedback/<int:feedback_id>/read')
@login_required
def mark_feedback_read(feedback_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    feedback = Feedback.query.get_or_404(feedback_id)
    feedback.status = '已读'
    db.session.commit()
    flash('已标记为已读')
    return redirect(request.referrer or url_for('admin_feedback'))

@app.route('/player/notifications')
@login_required
def player_notifications():
    if current_user.role != 'player':
        return redirect(url_for('admin_dashboard'))
    type_filter = request.args.get('type', '').strip()
    query = Notification.query.filter(
        Notification.receiver_type == 'player',
        Notification.receiver_id == current_user.id
    )
    if type_filter:
        query = query.filter(Notification.type == type_filter)
    notifications = query.order_by(Notification.created_at.desc()).all()
    return render_template('player/notifications.html', notifications=notifications, type_filter=type_filter)


@app.route('/player/notifications/read_all', methods=['POST'])
@login_required
def player_notifications_read_all():
    if current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    for n in Notification.query.filter_by(
        receiver_type='player', receiver_id=current_user.id, is_read=False
    ).all():
        n.is_read = True
    db.session.commit()
    flash('已全部标为已读')
    return redirect(url_for('player_notifications'))


@app.route('/player/notification/read/<int:notification_id>')
@login_required
def mark_player_notification_read(notification_id):
    n = Notification.query.get_or_404(notification_id)
    if current_user.role != 'player' or n.receiver_type != 'player' or n.receiver_id != current_user.id:
        flash('无权操作')
        return redirect(url_for('player_dashboard'))
    n.is_read = True
    db.session.commit()
    return redirect(request.referrer or url_for('player_notifications'))

@app.route('/rules')
@login_required
def rules():
    return render_template('rules.html')


# ---------- 游戏资讯（前台）---------
@app.route('/news')
def game_news_list():
    """游戏资讯列表"""
    game = request.args.get('game', '')
    query = GameNews.query.filter_by(is_published=True)
    if game:
        query = query.filter_by(game=game)
    news_list = query.order_by(GameNews.sort_order.desc(), GameNews.created_at.desc()).all()
    games = db.session.query(GameNews.game).filter(GameNews.game.isnot(None), GameNews.game != '', GameNews.is_published == True).distinct().all()
    games = [g[0] for g in games]
    return render_template('game_news_list.html', news_list=news_list, games=games, current_game=game)


@app.route('/news/<int:news_id>')
def game_news_detail(news_id):
    """游戏资讯详情"""
    news = GameNews.query.get_or_404(news_id)
    if not news.is_published:
        abort(404)
    return render_template('game_news_detail.html', news=news)


@app.route('/member/buy/<int:plan_id>')
def member_buy(plan_id):
    plan = MemberPlan.query.get_or_404(plan_id)
    if not session.get('customer_id'):
        flash('请先登录后再购买会员')
        return redirect(url_for('customer_query'))
    return render_template('member/buy.html', plan=plan)

@app.route('/member/create_order', methods=['POST'])
def member_create_order():
    plan_id = request.form.get('plan_id')
    plan = MemberPlan.query.get_or_404(plan_id)
    customer_id = session.get('customer_id')
    if not customer_id:
        flash('请先登录后再购买会员')
        return redirect(url_for('customer_query'))
    customer = Customer.query.get(customer_id)
    if not customer:
        session.pop('customer_id', None)
        flash('请重新登录')
        return redirect(url_for('customer_query'))

    # 不可降级：当前有效会员只能续期同档或升级
    membership = CustomerMember.query.filter_by(customer_id=customer.id).first()
    now = datetime.utcnow()
    if membership and membership.end_date and membership.end_date > now:
        if plan.price < membership.plan.price:
            flash('不可降级，请选择当前档位或更高档位')
            return redirect(url_for('index', _anchor='member-recharge'))

    order_no = f"M{int(datetime.utcnow().timestamp())}"
    order = MemberOrder(
        order_no=order_no,
        customer_id=customer.id,
        plan_id=plan.id,
        amount=plan.price,
        status='pending'
    )
    db.session.add(order)
    db.session.commit()

    return redirect(url_for('member_pay', order_id=order.id))

@app.route('/member/pay/<int:order_id>')
def member_pay(order_id):
    order = MemberOrder.query.get_or_404(order_id)
    if order.status == 'paid':
        flash('订单已支付')
        return redirect(url_for('member_order_detail', order_id=order.id))
    return render_template('member/pay.html', order=order)

@app.route('/member/pay/confirm/<int:order_id>', methods=['POST'])
def member_pay_confirm(order_id):
    order = MemberOrder.query.get_or_404(order_id)
    if order.status == 'paid':
        flash('订单已支付')
        return redirect(url_for('member_order_detail', order_id=order.id))

    customer = order.customer
    plan = order.plan
    now = datetime.utcnow()
    membership = CustomerMember.query.filter_by(customer_id=customer.id).first()

    # 不可降级：有效期内若选择更便宜套餐，拒绝并提示
    if membership and membership.end_date and membership.end_date > now:
        old_plan = membership.plan
        if membership.plan_id != plan.id and plan.price < old_plan.price:
            flash('不可降级，请选择当前档位或更高档位')
            return redirect(url_for('member_pay', order_id=order.id))

    order.status = 'paid'
    order.paid_at = now
    order.payment_method = '微信'

    if membership and membership.end_date and membership.end_date > now:
        # 当前仍在有效期内：续期或升级
        old_plan = membership.plan
        if membership.plan_id == plan.id:
            # 相同套餐：在原到期日上叠加天数
            membership.end_date = membership.end_date + timedelta(days=plan.duration_days)
            membership.is_active = True
            flash('支付成功，会员已续期！')
        else:
            # 不同套餐：视为升级/降级，新套餐立即生效
            membership.plan_id = plan.id
            membership.start_date = now
            membership.end_date = now + timedelta(days=plan.duration_days)
            membership.is_active = True
            if old_plan and plan.price > old_plan.price:
                flash('支付成功，已升级为更高档会员！')
            else:
                flash('支付成功，会员已切换为新套餐！')
    else:
        # 无有效会员或已过期：新开通
        if membership:
            membership.plan_id = plan.id
            membership.start_date = now
            membership.end_date = now + timedelta(days=plan.duration_days)
            membership.is_active = True
        else:
            membership = CustomerMember(
                customer_id=customer.id,
                plan_id=plan.id,
                start_date=now,
                end_date=now + timedelta(days=plan.duration_days)
            )
            db.session.add(membership)
        flash('支付成功，会员已开通！')

    # 购买会员的金额计入可消费余额，可用于后续代练订单支付
    customer.balance = (customer.balance or 0) + order.amount

    db.session.commit()
    return redirect(url_for('member_order_detail', order_id=order.id))

@app.route('/customer')
def customer_index():
    prices = Price.query.filter(
        db.or_(Price.service_type == '代肝', Price.service_type.is_(None))
    ).order_by(Price.game, Price.task_type).all()
    grouped_prices = {}
    phone = request.args.get('phone', '')
    customer = None
    customer_level = None
    discount_rate = 1.0
    membership = None
    if phone:
        customer = Customer.query.filter_by(phone=phone).first()
        if customer:
            # 优先使用当前会员套餐折扣（开通即生效），否则再用累计消费等级折扣
            membership = CustomerMember.query.filter_by(
                customer_id=customer.id, is_active=True
            ).first()
            if membership and membership.end_date and membership.end_date > datetime.utcnow():
                discount_rate = membership.plan.discount
            else:
                customer_level, discount_rate = get_level_and_discount(customer.total_spent)
    for p in prices:
        if p.game not in grouped_prices:
            grouped_prices[p.game] = []
        item = {'game': p.game, 'task_type': p.task_type, 'price': p.price, 'unit': p.unit or '元/次', 'remark': p.remark}
        item['member_price'] = round(p.price * discount_rate, 2) if discount_rate < 1.0 else p.price
        grouped_prices[p.game].append(item)
    return render_template('customer/index.html', grouped_prices=grouped_prices,
                          customer=customer, customer_level=customer_level, membership=membership, phone=phone,
                          price_table_images=list_price_table_images())

@app.route('/hot-players')
def hot_players():
    players_raw = User.query.filter_by(role='player', is_approved=True).order_by(User.player_name).all()
    stats = []
    for p in players_raw:
        completed = Order.query.filter_by(player_id=p.id, status='已完成').count()
        rating_rows = db.session.query(func.avg(Order.rating)).filter(
            Order.player_id == p.id,
            Order.status == '已完成',
            Order.rating.isnot(None)
        ).scalar()
        avg_rating = round(float(rating_rows or 0), 1) if rating_rows is not None else None
        env_photos = []
        equip_photos = []
        try:
            if p.environment_photos:
                env_photos = json.loads(p.environment_photos)
            if not isinstance(env_photos, list):
                env_photos = []
        except (TypeError, ValueError):
            pass
        try:
            if p.equipment_photos:
                equip_photos = json.loads(p.equipment_photos)
            if not isinstance(equip_photos, list):
                equip_photos = []
        except (TypeError, ValueError):
            pass
        stats.append({
            'player': p,
            'completed_count': completed,
            'avg_rating': avg_rating,
            'rating_count': Order.query.filter(
                Order.player_id == p.id, Order.status == '已完成', Order.rating.isnot(None)
            ).count(),
            'env_photos': env_photos,
            'equip_photos': equip_photos,
        })
    stats.sort(key=lambda x: (x['completed_count'], x['avg_rating'] or 0), reverse=True)
    return render_template('customer/hot_players.html', players=stats)


@app.route('/customer/peiwan')
def customer_peiwan_index():
    """陪玩价格表"""
    prices = Price.query.filter_by(service_type='陪玩').order_by(Price.game, Price.task_type).all()
    grouped_prices = {}
    phone = request.args.get('phone', '')
    customer = None
    discount_rate = 1.0
    membership = None
    if phone:
        customer = Customer.query.filter_by(phone=phone).first()
        if customer:
            membership = CustomerMember.query.filter_by(customer_id=customer.id, is_active=True).first()
            if membership and membership.end_date and membership.end_date > datetime.utcnow():
                discount_rate = membership.plan.discount
            else:
                _, discount_rate = get_level_and_discount(customer.total_spent)
    for p in prices:
        if p.game not in grouped_prices:
            grouped_prices[p.game] = []
        item = {'game': p.game, 'task_type': p.task_type, 'price': p.price, 'unit': p.unit or '元/小时', 'remark': getattr(p, 'remark', None)}
        item['member_price'] = round(p.price * discount_rate, 2) if discount_rate < 1.0 else p.price
        grouped_prices[p.game].append(item)
    return render_template('customer/peiwan_index.html', grouped_prices=grouped_prices,
                          customer=customer, membership=membership, phone=phone)


@app.route('/customer/peiwan/order', methods=['GET', 'POST'])
def customer_peiwan_order():
    if request.method == 'POST':
        customer_id = session.get('customer_id')
        if customer_id:
            customer = Customer.query.get(customer_id)
            if not customer:
                session.pop('customer_id', None)
                flash('请重新登录')
                return redirect(url_for('customer_query'))
            phone = customer.phone
            name = request.form.get('name') or customer.name
        else:
            phone = request.form.get('phone', '')
            name = request.form.get('name', '')
        game = request.form.get('game', '')
        task_type = request.form.get('task_type', '')
        description = request.form.get('description', '')
        try:
            duration_hours = float(request.form.get('duration_hours', 1) or 1)
            duration_hours = max(0.5, min(24, duration_hours))
        except (TypeError, ValueError):
            duration_hours = 1.0
        coupon_code = request.form.get('coupon_code', '').strip().upper()

        if not customer_id:
            customer = Customer.query.filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, name=name)
                db.session.add(customer)
                db.session.commit()

        price_item = Price.query.filter_by(game=game, task_type=task_type, service_type='陪玩').first()
        if not price_item:
            flash('所选陪玩项目暂无定价，请从陪玩价格表选择')
            return redirect(url_for('customer_peiwan_order'))
        unit_price = price_item.price
        original_price = round(unit_price * duration_hours, 2)
        member_discount = 1.0
        if customer:
            membership = CustomerMember.query.filter_by(customer_id=customer.id, is_active=True).first()
            if membership and membership.end_date and membership.end_date > datetime.utcnow():
                member_discount = membership.plan.discount
        price_after_member = round(original_price * member_discount, 2)
        discount_amount = 0
        coupon_obj = None
        if coupon_code:
            coupon_obj = Coupon.query.filter_by(code=coupon_code, used_by=None).first()
            if coupon_obj and (not coupon_obj.valid_date or coupon_obj.valid_date >= datetime.utcnow()) and price_after_member >= (coupon_obj.min_amount or 0):
                if coupon_obj.discount_type == 'percent':
                    discount_amount = price_after_member * coupon_obj.discount_value
                else:
                    discount_amount = min(coupon_obj.discount_value, price_after_member)
            else:
                coupon_obj = None
        final_price = max(0, price_after_member - discount_amount)
        order = Order(
            order_no=f"PW{int(datetime.utcnow().timestamp())}",
            game=game,
            task_type=task_type,
            customer_price=final_price,
            original_price=original_price,
            member_discount=member_discount,
            player_price=0,
            status='待确认',
            notes=(description or '') + f' [陪玩时长{duration_hours}小时]',
            customer_id=customer.id,
            discount_amount=discount_amount,
            service_type='陪玩',
            duration_hours=duration_hours
        )
        db.session.add(order)
        db.session.flush()
        if coupon_obj:
            order.coupon_id = coupon_obj.id
            coupon_obj.used_by = customer.id
            coupon_obj.used_at = datetime.utcnow()
            coupon_obj.order_id = order.id
        db.session.commit()
        flash('陪玩订单提交成功，请完成支付')
        return redirect(url_for('customer_pay', order_id=order.id))

    games = db.session.query(Price.game).filter_by(service_type='陪玩').distinct().all()
    price_data = {}
    for g in games:
        game = g[0]
        tasks = Price.query.filter_by(game=game, service_type='陪玩').all()
        price_data[game] = [{'task_type': t.task_type, 'price': t.price, 'unit': t.unit or '元/小时'} for t in tasks]
    current_customer = None
    if session.get('customer_id'):
        current_customer = Customer.query.get(session['customer_id'])
    return render_template('customer/peiwan_order.html', games=games, price_data=price_data, current_customer=current_customer)


@app.route('/customer/order', methods=['GET', 'POST'])
def customer_order():
    if request.method == 'POST':
        # 已登录顾客：使用 session 中的顾客，不再用表单手机号
        customer_id = session.get('customer_id')
        if customer_id:
            customer = Customer.query.get(customer_id)
            if not customer:
                session.pop('customer_id', None)
                flash('请重新登录')
                return redirect(url_for('customer_query'))
            phone = customer.phone
            name = request.form.get('name') or customer.name
        else:
            phone = request.form['phone']
            name = request.form.get('name')
        game = request.form['game']
        task_type = request.form['task_type']
        description = request.form['description']
        points_used = int(request.form.get('points_used', 0))
        coupon_code = request.form.get('coupon_code', '').strip().upper()
        screenshot = None
        if 'screenshot' in request.files:
            file = request.files['screenshot']
            if file.filename:
                filename = secure_filename(f"customer_{int(datetime.utcnow().timestamp())}_{file.filename}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                screenshot = filename

        if not customer_id:
            customer = Customer.query.filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, name=name)
                db.session.add(customer)
                db.session.commit()

        price_item = Price.query.filter_by(game=game, task_type=task_type).filter(
            db.or_(Price.service_type == '代肝', Price.service_type.is_(None))
        ).first()
        if not price_item:
            flash('所选游戏或任务类型暂无定价，请联系管理员')
            return redirect(url_for('customer_order'))

        original_price = price_item.price
        member_discount = 1.0
        if customer:
            membership = CustomerMember.query.filter_by(customer_id=customer.id, is_active=True).first()
            if membership and membership.end_date and membership.end_date > datetime.utcnow():
                member_discount = membership.plan.discount
        price_after_member = round(original_price * member_discount, 2)

        discount_amount = 0
        coupon_obj = None

        points_deduction = 0
        if points_used > 0:
            avail_points = customer.points or 0
            if avail_points < points_used:
                points_used = 0
                flash('积分不足，已取消积分抵扣')
            else:
                points_deduction = min(points_used * 0.01, price_after_member)
                discount_amount += points_deduction

        if coupon_code:
            coupon_obj = Coupon.query.filter_by(code=coupon_code, used_by=None).first()
            if coupon_obj:
                if coupon_obj.valid_date and coupon_obj.valid_date < datetime.utcnow():
                    coupon_obj = None
                    flash('优惠券已过期')
                elif price_after_member < (coupon_obj.min_amount or 0):
                    coupon_obj = None
                    flash(f'未达到优惠券最低消费{coupon_obj.min_amount}元')
                else:
                    if coupon_obj.discount_type == 'percent':
                        coupon_discount = price_after_member * coupon_obj.discount_value
                    else:
                        coupon_discount = min(coupon_obj.discount_value, price_after_member - discount_amount)
                    discount_amount += coupon_discount
            else:
                flash('优惠券无效或已被使用')

        final_price = max(0, price_after_member - discount_amount)
        customer.points = (customer.points or 0) - points_used

        order = Order(
            order_no=f"ORD{int(datetime.utcnow().timestamp())}",
            game=game,
            task_type=task_type,
            customer_price=final_price,
            original_price=original_price,
            member_discount=member_discount,
            player_price=0,
            status='待确认',
            screenshot=screenshot,
            notes=description,
            customer_id=customer.id,
            points_used=points_used,
            discount_amount=discount_amount,
            service_type='代肝'
        )
        db.session.add(order)
        db.session.flush()
        if coupon_obj:
            order.coupon_id = coupon_obj.id
            coupon_obj.used_by = customer.id
            coupon_obj.used_at = datetime.utcnow()
            coupon_obj.order_id = order.id
        db.session.commit()
        flash('订单提交成功，请完成支付')
        return redirect(url_for('customer_pay', order_id=order.id))

    games = db.session.query(Price.game).filter(
        db.or_(Price.service_type == '代肝', Price.service_type.is_(None))
    ).distinct().all()
    price_data = {}
    for game_tuple in games:
        game = game_tuple[0]
        tasks = Price.query.filter_by(game=game).filter(
            db.or_(Price.service_type == '代肝', Price.service_type.is_(None))
        ).all()
        price_data[game] = [{'task_type': t.task_type, 'price': t.price} for t in tasks]
    default_games = ['原神', '鸣潮', '星铁', '终末地', '三角洲', '永劫无间']
    for g in default_games:
        if g not in price_data:
            price_data[g] = []
        if not any(x[0] == g for x in games):
            games = list(games) + [(g,)]
    current_customer = None
    if session.get('customer_id'):
        current_customer = Customer.query.get(session['customer_id'])
    return render_template('customer/order.html', games=games, price_data=price_data, current_customer=current_customer)


def _customer_has_annual_or_above(customer):
    """是否拥有年卡或终身会员（私人定制仅限年卡及以上）"""
    if not customer:
        return False
    m = CustomerMember.query.filter_by(customer_id=customer.id, is_active=True).first()
    if not m or not m.end_date:
        return False
    if m.end_date <= datetime.utcnow():
        return False
    return (m.plan and m.plan.duration_days >= 365)


@app.route('/customer/order/custom', methods=['GET', 'POST'])
def customer_order_custom():
    """顾客自定义需求与报价：提交后为意向，打手接单后顾客支付，支付成功后才创建订单"""
    if request.method == 'POST':
        customer_id = session.get('customer_id')
        if customer_id:
            customer = Customer.query.get(customer_id)
            if not customer:
                session.pop('customer_id', None)
                flash('请重新登录')
                return redirect(url_for('customer_query'))
            phone = customer.phone
            name = request.form.get('name') or customer.name
        else:
            phone = request.form.get('phone', '').strip()
            name = request.form.get('name', '').strip()
            if not phone:
                flash('请填写手机号')
                return redirect(url_for('customer_order_custom'))
        game = request.form.get('game', '').strip()
        task_type = request.form.get('task_type', '').strip()
        description = request.form.get('description', '').strip()
        try:
            offered_price = float(request.form.get('offered_price', 0))
        except (TypeError, ValueError):
            offered_price = 0
        if not game or not task_type or not description:
            flash('请填写游戏、任务类型和需求描述')
            return redirect(url_for('customer_order_custom'))
        if offered_price <= 0:
            flash('请填写您愿意支付的价格（大于0）')
            return redirect(url_for('customer_order_custom'))

        if not customer_id:
            customer = Customer.query.filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, name=name)
                db.session.add(customer)
                db.session.commit()

        if not _customer_has_annual_or_above(customer):
            flash('私人定制仅限年卡及以上会员使用，请先升级会员后再提交。')
            return redirect(url_for('index', _anchor='member-recharge'))

        screenshot = None
        if 'screenshot' in request.files:
            file = request.files['screenshot']
            if file.filename:
                filename = secure_filename(f"custom_{int(datetime.utcnow().timestamp())}_{file.filename}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                screenshot = filename

        req = CustomOfferRequest(
            request_no=f"REQ{int(datetime.utcnow().timestamp())}",
            customer_id=customer.id,
            game=game,
            task_type=task_type,
            notes=description,
            offered_price=offered_price,
            screenshot=screenshot,
            status='待接单',
        )
        db.session.add(req)
        db.session.commit()
        flash('您的需求已发布，打手可浏览并接单。接单后请完成支付，支付成功后才生成订单。')
        return redirect(url_for('customer_custom_request_detail', request_id=req.id))

    current_customer = None
    if session.get('customer_id'):
        current_customer = Customer.query.get(session['customer_id'])
    if not current_customer:
        return render_template('customer/order_custom_restrict.html', reason='login')
    if not _customer_has_annual_or_above(current_customer):
        return render_template('customer/order_custom_restrict.html', reason='upgrade')
    games = db.session.query(Price.game).distinct().all()
    games = [g[0] for g in games]
    default_games = ['原神', '鸣潮', '星铁', '终末地', '三角洲', '永劫无间']
    for g in default_games:
        if g not in games:
            games.append(g)
    return render_template('customer/order_custom.html', games=games, current_customer=current_customer)


@app.route('/customer/request', methods=['GET', 'POST'])
def customer_request_submit():
    """没有价格表·提交需求与报价：打手接单后支付，支付成功生成订单。"""
    if request.method == 'POST':
        customer_id = session.get('customer_id')
        if customer_id:
            customer = Customer.query.get(customer_id)
            if not customer:
                session.pop('customer_id', None)
                customer_id = None
            else:
                phone = customer.phone
                name = request.form.get('name') or customer.name
        if not customer_id:
            phone = request.form.get('phone', '').strip()
            name = request.form.get('name', '').strip()
            if not phone:
                flash('请填写手机号')
                return redirect(url_for('customer_request_submit'))
        game = request.form.get('game', '').strip()
        task_type = request.form.get('task_type', '').strip()
        description = request.form.get('description', '').strip()
        try:
            offered_price = float(request.form.get('offered_price', 0))
        except (TypeError, ValueError):
            offered_price = 0
        if not game or not task_type or not description:
            flash('请填写游戏、任务类型和需求描述')
            return redirect(url_for('customer_request_submit'))
        if offered_price <= 0:
            flash('请填写您愿意支付的价格（大于0）')
            return redirect(url_for('customer_request_submit'))
        if not customer_id:
            customer = Customer.query.filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, name=name)
                db.session.add(customer)
                db.session.commit()
        screenshot = None
        if 'screenshot' in request.files:
            file = request.files['screenshot']
            if file.filename:
                filename = secure_filename(f"custom_{int(datetime.utcnow().timestamp())}_{file.filename}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                screenshot = filename
        # 二次元且未上架：仅推送给擅长该游戏的打手、20%平台费、完成后录入平台价
        price_games = set(g[0] for g in db.session.query(Price.game).distinct().all())
        default_games_set = {'原神', '鸣潮', '星铁', '终末地', '三角洲', '永劫无间'}
        games_set = price_games | default_games_set
        is_anime_no_display = (game in ANIME_GAMES_CUSTOMER_OFFER and game not in games_set)
        req = CustomOfferRequest(
            request_no=f"REQ{int(datetime.utcnow().timestamp())}",
            customer_id=customer.id,
            game=game,
            task_type=task_type,
            notes=description,
            offered_price=offered_price,
            screenshot=screenshot,
            status='待接单',
            is_anime_no_display=is_anime_no_display,
        )
        db.session.add(req)
        db.session.commit()
        flash('您的需求已发布，打手可浏览并接单。接单后请完成支付，支付成功后才生成订单。')
        return redirect(url_for('customer_custom_request_detail', request_id=req.id))

    current_customer = None
    if session.get('customer_id'):
        current_customer = Customer.query.get(session['customer_id'])
    games = db.session.query(Price.game).distinct().all()
    games = [g[0] for g in games]
    default_games = ['原神', '鸣潮', '星铁', '终末地', '三角洲', '永劫无间']
    for g in default_games:
        if g not in games:
            games.append(g)
    # 二次元游戏：仅“找不到价格表”时列出，排除已上架游戏
    anime_games = [g for g in ANIME_GAMES_CUSTOMER_OFFER if g not in games]
    return render_template('customer/request_submit.html', games=games, anime_games=anime_games, current_customer=current_customer)


@app.route('/customer/custom_request/<int:request_id>')
def customer_custom_request_detail(request_id):
    """顾客自定义报价意向详情：待接单 / 已接单请支付"""
    req = CustomOfferRequest.query.get_or_404(request_id)
    return render_template('customer/custom_request_detail.html', req=req)


@app.route('/customer/custom_pay/<int:request_id>', methods=['GET'])
def customer_custom_pay(request_id):
    """意向支付页（仅已接单的意向可支付）"""
    req = CustomOfferRequest.query.get_or_404(request_id)
    if req.status == '已支付' and req.order_id:
        flash('已支付，请查看订单')
        return redirect(url_for('customer_order_detail', order_id=req.order_id))
    if req.status != '已接单':
        flash('仅打手接单后可支付' if req.status == '待接单' else '该意向已支付')
        return redirect(url_for('customer_custom_request_detail', request_id=req.id))
    return render_template('customer/custom_pay.html', req=req)


@app.route('/customer/custom_pay/confirm/<int:request_id>', methods=['POST'])
def customer_custom_pay_confirm(request_id):
    """意向支付确认：支付成功后创建订单，并通知打手"""
    req = CustomOfferRequest.query.get_or_404(request_id)
    if req.status == '已支付' and req.order_id:
        flash('已支付')
        return redirect(url_for('customer_order_detail', order_id=req.order_id))
    if req.status != '已接单':
        flash('仅打手接单后可支付')
        return redirect(url_for('customer_custom_request_detail', request_id=req.id))

    order = Order(
        order_no=f"ORD{int(datetime.utcnow().timestamp())}",
        game=req.game,
        task_type=req.task_type,
        customer_price=req.offered_price,
        original_price=req.offered_price,
        member_discount=1.0,
        player_price=0,
        status='进行中',
        screenshot=req.screenshot,
        notes=req.notes,
        customer_id=req.customer_id,
        payment_status='已支付',
        paid_at=datetime.utcnow(),
        is_custom_offer=True,
        player_id=req.player_id,
    )
    if req.player_id:
        player = User.query.get(req.player_id)
        if player:
            if getattr(req, 'is_anime_no_display', False):
                order.player_price = round(req.offered_price * (1 - ANIME_CUSTOMER_OFFER_COMMISSION), 2)
            else:
                reward = calculate_player_price(req.offered_price, player)
                order.player_price = reward if reward is not None else 0
    db.session.add(order)
    db.session.flush()
    payment = Payment(order_id=order.id, amount=order.customer_price, method='微信', status='成功')
    db.session.add(payment)
    req.status = '已支付'
    req.order_id = order.id
    if req.customer_id:
        db.session.add(Notification(
            customer_id=req.customer_id,
            order_id=order.id,
            type='支付成功',
            content=f'您的订单 {order.order_no} 已支付成功',
            receiver_type='customer',
            receiver_id=req.customer_id
        ))
    if req.player_id:
        db.session.add(Notification(
            order_id=order.id,
            type='顾客已支付',
            content=f'订单 {order.order_no} 顾客已支付，请开始处理',
            receiver_type='player',
            receiver_id=req.player_id
        ))
    db.session.commit()
    flash('支付成功！订单已生成。')
    return redirect(url_for('customer_order_detail', order_id=order.id))


@app.route('/customer/query', methods=['GET', 'POST'])
def customer_query():
    if request.method == 'POST':
        phone = (request.form.get('phone') or '').strip()
        order_no = (request.form.get('order_no') or '').strip()
        if not phone or not order_no:
            flash('请填写手机号和订单号')
            return redirect(url_for('customer_query'))
        order = Order.query.filter_by(order_no=order_no).first()
        if order and order.customer and order.customer.phone == phone:
            session['customer_id'] = order.customer_id
            session['customer_phone'] = order.customer.phone
            return redirect(url_for('customer_order_detail', order_id=order.id))
        flash('未找到匹配的订单，请核对手机号与订单号')
        return redirect(url_for('customer_query'))
    return render_template('customer/query.html')

@app.route('/customer/order/<int:order_id>')
def customer_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('customer/order_detail.html', order=order)

@app.route('/customer/pay/<int:order_id>', methods=['GET'])
def customer_pay(order_id):
    order = Order.query.get_or_404(order_id)
    if order.payment_status == '已支付':
        flash('订单已支付，无需重复支付')
        return redirect(url_for('customer_order_detail', order_id=order.id))
    customer = None
    if order.customer_id:
        customer = Customer.query.get(order.customer_id)
    return render_template('customer/pay.html', order=order, customer=customer)

@app.route('/customer/pay/confirm/<int:order_id>', methods=['POST'])
def customer_pay_confirm(order_id):
    order = Order.query.get_or_404(order_id)
    if order.payment_status == '已支付':
        flash('订单已支付')
        return redirect(url_for('customer_order_detail', order_id=order.id))

    customer = Customer.query.get(order.customer_id) if order.customer_id else None
    balance_used = 0
    try:
        balance_used = float(request.form.get('balance_used') or 0)
    except (TypeError, ValueError):
        balance_used = 0

    if balance_used > 0 and customer and (order.balance_used or 0) == 0:
        avail = customer.balance or 0
        need = order.customer_price
        balance_used = min(balance_used, avail, need)
        if balance_used <= 0:
            balance_used = 0
        else:
            customer.balance = avail - balance_used
            order.balance_used = balance_used

    if (order.balance_used or 0) >= order.customer_price:
        order.payment_status = '已支付'
        order.paid_at = datetime.utcnow()
        order.status = '待分配'
        payment = Payment(order_id=order.id, amount=order.customer_price, method='余额' if (order.balance_used or 0) >= order.customer_price else '微信', status='成功')
        db.session.add(payment)
        if order.customer_id:
            db.session.add(Notification(
                customer_id=order.customer_id, order_id=order.id, type='支付成功',
                content=f'您的订单 {order.order_no} 已支付成功，正在等待分配打手',
                receiver_type='customer', receiver_id=order.customer_id
            ))
        if order.player_id:
            db.session.add(Notification(
                order_id=order.id, type='顾客已支付', content=f'订单 {order.order_no} 顾客已支付，请开始处理',
                receiver_type='player', receiver_id=order.player_id
            ))
        db.session.commit()
        auto_assign_order(order.id)
        flash('支付成功！订单已提交，我们将尽快为您安排打手。')
        return redirect(url_for('customer_order_detail', order_id=order.id))

    if balance_used > 0 and (order.balance_used or 0) < order.customer_price:
        remain = order.customer_price - (order.balance_used or 0)
        db.session.commit()
        flash(f'已使用余额抵扣 ￥{order.balance_used:.2f}，还需支付 ￥{remain:.2f}，请扫码后点击「我已支付」')
        return redirect(url_for('customer_pay', order_id=order.id))

    order.payment_status = '已支付'
    order.paid_at = datetime.utcnow()
    order.status = '待分配'
    pay_method = '余额+微信' if (order.balance_used or 0) > 0 else '微信'
    payment = Payment(order_id=order.id, amount=order.customer_price, method=pay_method, status='成功')
    db.session.add(payment)

    if order.customer_id:
        notification = Notification(
            customer_id=order.customer_id,
            order_id=order.id,
            type='支付成功',
            content=f'您的订单 {order.order_no} 已支付成功，正在等待分配打手',
            receiver_type='customer',
            receiver_id=order.customer_id
        )
        db.session.add(notification)

    # 若打手已接单（如顾客报价单），支付完成后通知打手
    if order.player_id:
        db.session.add(Notification(
            order_id=order.id,
            type='顾客已支付',
            content=f'订单 {order.order_no} 顾客已支付，请开始处理',
            receiver_type='player',
            receiver_id=order.player_id
        ))

    db.session.commit()
    auto_assign_order(order.id)
    flash('支付成功！订单已提交，我们将尽快为您安排打手。')
    return redirect(url_for('customer_order_detail', order_id=order.id))

@app.route('/customer/rate/<int:order_id>', methods=['GET', 'POST'])
def customer_rate(order_id):
    order = Order.query.get_or_404(order_id)
    if order.status != '已完成':
        flash('订单尚未完成，暂不能评价')
        return redirect(url_for('customer_order_detail', order_id=order.id))
    if request.method == 'POST':
        order.rating = int(request.form['rating'])
        order.comment = request.form['comment']
        db.session.commit()
        flash('感谢您的评价！')
        return redirect(url_for('customer_order_detail', order_id=order.id))
    return render_template('customer/rate.html', order=order)

@app.route('/customer/notifications')
def customer_notifications():
    customer_id = request.args.get('customer_id')
    if not customer_id:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    customer = Customer.query.get(customer_id)
    if not customer:
        flash('顾客不存在')
        return redirect(url_for('customer_query'))
    notifications = Notification.query.filter_by(customer_id=customer_id).order_by(Notification.created_at.desc()).all()
    return render_template('customer/notifications.html', notifications=notifications, customer=customer)

@app.route('/customer/notification/read/<int:notification_id>')
def mark_notification_read(notification_id):
    n = Notification.query.get_or_404(notification_id)
    n.is_read = True
    db.session.commit()
    return redirect(request.referrer or url_for('customer_notifications', customer_id=n.customer_id))


@app.route('/customer/login', methods=['GET', 'POST'])
def customer_login():
    if request.method == 'POST':
        phone = (request.form.get('phone') or '').strip()
        password = request.form.get('password') or ''
        if not phone or not password:
            flash('请填写手机号和密码')
            return redirect(url_for('customer_login'))
        customer = Customer.query.filter_by(phone=phone).first()
        if customer and customer.password and check_password_hash(customer.password, password):
            session['customer_id'] = customer.id
            session['customer_phone'] = customer.phone
            flash('登录成功')
            return redirect(url_for('customer_index'))
        if customer and not customer.password:
            customer.password = generate_password_hash(password)
            db.session.commit()
            session['customer_id'] = customer.id
            session['customer_phone'] = customer.phone
            flash('注册并登录成功')
            return redirect(url_for('customer_index'))
        if customer and customer.password:
            flash('密码错误，请重试')
            return redirect(url_for('customer_login'))
        customer = Customer(phone=phone, name=phone)
        customer.password = generate_password_hash(password)
        db.session.add(customer)
        db.session.commit()
        session['customer_id'] = customer.id
        session['customer_phone'] = customer.phone
        flash('注册并登录成功')
        return redirect(url_for('customer_index'))
    return render_template('customer/login.html')


@app.route('/customer/logout')
def customer_logout():
    session.pop('customer_id', None)
    session.pop('customer_phone', None)
    flash('已退出')
    return redirect(url_for('customer_index'))


@app.route('/customer/profile')
def customer_profile():
    phone = request.args.get('phone')
    if not phone:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        flash('顾客不存在')
        return redirect(url_for('index'))
    membership = CustomerMember.query.filter_by(customer_id=customer.id, is_active=True).first()
    return render_template('customer/profile.html', customer=customer, membership=membership)


@app.route('/customer/orders')
def customer_orders():
    phone = request.args.get('phone')
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        flash('请先登录或查询订单')
        return redirect(url_for('customer_query'))
    status_filter = request.args.get('status', '').strip()
    query = Order.query.filter_by(customer_id=customer.id)
    if status_filter:
        query = query.filter(Order.status == status_filter)
    query = query.order_by(Order.created_at.desc())
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = query.paginate(page=page, per_page=per_page)
    orders = pagination.items
    return render_template('customer/orders.html', orders=orders, pagination=pagination, customer=customer, status_filter=status_filter)


@app.route('/customer/requests')
def customer_my_requests():
    """我的报价意向（支付成功后才生成订单）"""
    phone = request.args.get('phone')
    if not phone and session.get('customer_id'):
        customer = Customer.query.get(session['customer_id'])
        if customer:
            phone = customer.phone
    if not phone:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    requests = CustomOfferRequest.query.filter_by(customer_id=customer.id).order_by(CustomOfferRequest.created_at.desc()).all()
    return render_template('customer/my_requests.html', requests=requests, customer=customer)


@app.route('/customer/my_messages')
def customer_my_messages():
    """我的客服留言（仅登录顾客可看，含客服回复）"""
    customer_id = session.get('customer_id')
    if not customer_id:
        flash('请先登录后查看我的留言')
        return redirect(url_for('customer_query'))
    customer = Customer.query.get(customer_id)
    if not customer:
        session.pop('customer_id', None)
        return redirect(url_for('customer_query'))
    messages = CustomerServiceMessage.query.filter_by(customer_id=customer.id).order_by(CustomerServiceMessage.created_at.desc()).all()
    return render_template('customer/my_messages.html', messages=messages, customer=customer)


# ---------- 顾客给打手赠送礼物（虚拟礼物 + 扫码支付）---------

@app.route('/customer/gift/send', methods=['GET', 'POST'])
def customer_gift_send():
    customer_id = session.get('customer_id')
    if not customer_id:
        flash('请先登录后再赠送礼物')
        return redirect(url_for('customer_query'))
    customer = Customer.query.get(customer_id)
    if not customer:
        session.pop('customer_id', None)
        flash('请重新登录')
        return redirect(url_for('customer_query'))
    players = User.query.filter_by(role='player', is_approved=True).order_by(User.player_name).all()
    products = GiftProduct.query.filter_by(is_active=True).order_by(GiftProduct.sort_order, GiftProduct.id).all()
    if not players:
        flash('暂无可赠送的打手')
        return redirect(url_for('customer_profile', phone=customer.phone))
    if not products:
        flash('暂无可选的礼物')
        return redirect(url_for('customer_profile', phone=customer.phone))
    if request.method == 'POST':
        player_id = request.form.get('player_id', type=int)
        product_id = request.form.get('gift_product_id', type=int)
        message = (request.form.get('message') or '').strip()[:500]
        product = GiftProduct.query.filter_by(id=product_id, is_active=True).first()
        player = User.query.filter_by(id=player_id, role='player', is_approved=True).first()
        if not product or not player:
            flash('请选择有效的打手和礼物')
            return redirect(url_for('customer_gift_send'))
        order_no = f"G{int(datetime.utcnow().timestamp())}"
        pay_token = secrets.token_urlsafe(32)
        order = GiftOrder(
            order_no=order_no,
            customer_id=customer.id,
            player_id=player.id,
            gift_product_id=product.id,
            amount=product.price,
            status='pending',
            message=message or None,
            pay_token=pay_token
        )
        db.session.add(order)
        db.session.commit()
        return redirect(url_for('customer_gift_pay', order_id=order.id))
    return render_template('customer/gift_send.html', customer=customer, players=players, products=products)


@app.route('/customer/gift/pay/<int:order_id>')
def customer_gift_pay(order_id):
    customer_id = session.get('customer_id')
    if not customer_id:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    order = GiftOrder.query.get_or_404(order_id)
    if order.customer_id != customer_id:
        flash('无权查看该订单')
        return redirect(url_for('customer_gifts_sent'))
    if order.status == 'paid':
        flash('该订单已支付')
        return redirect(url_for('customer_gifts_sent'))
    # 支付确认链接（扫码后打开此链接确认支付）
    confirm_url = url_for('customer_gift_pay_confirm', order_no=order.order_no, token=order.pay_token, _external=True)
    return render_template('customer/gift_pay.html', order=order, confirm_url=confirm_url)


@app.route('/customer/gift/pay/confirm', methods=['GET', 'POST'])
def customer_gift_pay_confirm():
    order_no = request.args.get('order_no')
    token = request.args.get('token')
    if not order_no or not token:
        flash('链接无效')
        return redirect(url_for('index'))
    order = GiftOrder.query.filter_by(order_no=order_no, pay_token=token).first()
    if not order:
        flash('订单不存在或链接已失效')
        return redirect(url_for('index'))
    if order.status == 'paid':
        return render_template('customer/gift_pay_done.html', order=order, already_paid=True)
    if request.method == 'POST' or request.args.get('confirm') == '1':
        order.status = 'paid'
        order.paid_at = datetime.utcnow()
        order.pay_token = None  # 一次性链接
        gift = CustomerGift(
            customer_id=order.customer_id,
            player_id=order.player_id,
            gift_product_id=order.gift_product_id,
            amount=order.amount,
            message=order.message
        )
        db.session.add(gift)
        n = Notification(
            type='顾客赠送礼物',
            content=f'顾客 {order.customer.name or order.customer.phone} 向您赠送了【{order.gift_product.name}】￥{order.amount}',
            receiver_type='player',
            receiver_id=order.player_id
        )
        db.session.add(n)
        db.session.commit()
        return render_template('customer/gift_pay_done.html', order=order, already_paid=False)
    return render_template('customer/gift_pay_confirm.html', order=order)


@app.route('/customer/gifts/sent')
def customer_gifts_sent():
    customer_id = session.get('customer_id')
    if not customer_id:
        flash('请先登录')
        return redirect(url_for('customer_query'))
    customer = Customer.query.get(customer_id)
    if not customer:
        return redirect(url_for('customer_query'))
    gifts = CustomerGift.query.filter_by(customer_id=customer.id).order_by(CustomerGift.created_at.desc()).all()
    return render_template('customer/gifts_sent.html', customer=customer, gifts=gifts)


@app.route('/player/gifts')
def player_gifts():
    if not current_user.is_authenticated or current_user.role != 'player':
        return redirect(url_for('player_dashboard'))
    try:
        gifts = CustomerGift.query.filter_by(player_id=current_user.id).order_by(CustomerGift.created_at.desc()).all()
    except Exception as e:
        if 'no such column' in str(e).lower() or 'operationalerror' in str(type(e).__name__).lower():
            try:
                from sqlalchemy import text
                t_cg = getattr(CustomerGift, '__tablename__', 'customer_gift')
                t_gp = getattr(GiftProduct, '__tablename__', 'gift_product')
                for sql in [
                    f'ALTER TABLE {t_cg} ADD COLUMN message TEXT',
                    f'ALTER TABLE {t_gp} ADD COLUMN is_active INTEGER DEFAULT 1',
                    f'ALTER TABLE {t_gp} ADD COLUMN description TEXT',
                    f'ALTER TABLE {t_gp} ADD COLUMN sort_order INTEGER DEFAULT 0',
                    f'ALTER TABLE {t_gp} ADD COLUMN icon VARCHAR(30)',
                    f'ALTER TABLE {t_gp} ADD COLUMN created_at DATETIME',
                ]:
                    try:
                        with db.engine.connect() as conn:
                            conn.execute(text(sql))
                            conn.commit()
                    except Exception:
                        pass
                db.session.expire_all()
                gifts = CustomerGift.query.filter_by(player_id=current_user.id).order_by(CustomerGift.created_at.desc()).all()
            except Exception:
                flash('加载礼物列表失败，请刷新页面或联系管理员')
                gifts = []
        else:
            raise
    return render_template('player/gifts.html', gifts=gifts)


# ---------- 管理端：礼物商品与礼物订单 ----------
@app.route('/admin/gift-products')
@login_required
def admin_gift_products():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    products = GiftProduct.query.order_by(GiftProduct.sort_order, GiftProduct.id).all()
    return render_template('admin/gift_products.html', products=products)


@app.route('/admin/gift-product/add', methods=['GET', 'POST'])
@login_required
def admin_gift_product_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        price = request.form.get('price')
        icon = (request.form.get('icon') or 'fa-gift').strip()
        description = (request.form.get('description') or '').strip()[:200]
        sort_order = request.form.get('sort_order', type=int) or 0
        if not name:
            flash('请填写礼物名称')
            return redirect(url_for('admin_gift_product_add'))
        try:
            price = float(price)
            if price < 0:
                raise ValueError('价格不能为负')
        except (TypeError, ValueError):
            flash('请填写有效价格')
            return redirect(url_for('admin_gift_product_add'))
        product = GiftProduct(
            name=name,
            price=price,
            icon=icon or 'fa-gift',
            description=description or None,
            sort_order=sort_order,
            is_active=True
        )
        db.session.add(product)
        db.session.commit()
        flash('礼物商品已添加')
        return redirect(url_for('admin_gift_products'))
    return render_template('admin/gift_product_form.html', product=None)


@app.route('/admin/gift-product/edit/<int:product_id>', methods=['GET', 'POST'])
@login_required
def admin_gift_product_edit(product_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    product = GiftProduct.query.get_or_404(product_id)
    if request.method == 'POST':
        product.name = (request.form.get('name') or product.name).strip()
        try:
            product.price = float(request.form.get('price', product.price))
            if product.price < 0:
                raise ValueError('价格不能为负')
        except (TypeError, ValueError):
            flash('请填写有效价格')
            return redirect(url_for('admin_gift_product_edit', product_id=product_id))
        product.icon = (request.form.get('icon') or product.icon or 'fa-gift').strip()
        product.description = (request.form.get('description') or '')[:200] or None
        product.sort_order = request.form.get('sort_order', type=int) or 0
        db.session.commit()
        flash('礼物商品已更新')
        return redirect(url_for('admin_gift_products'))
    return render_template('admin/gift_product_form.html', product=product)


@app.route('/admin/gift-product/toggle/<int:product_id>')
@login_required
def admin_gift_product_toggle(product_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    product = GiftProduct.query.get_or_404(product_id)
    product.is_active = not product.is_active
    db.session.commit()
    flash('已' + ('上架' if product.is_active else '下架'))
    return redirect(url_for('admin_gift_products'))


@app.route('/admin/gift-product/delete/<int:product_id>')
@login_required
def admin_gift_product_delete(product_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    product = GiftProduct.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash('礼物商品已删除')
    return redirect(url_for('admin_gift_products'))


@app.route('/admin/gift-orders')
@login_required
def admin_gift_orders():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    orders = GiftOrder.query.order_by(GiftOrder.created_at.desc()).all()
    paid_orders = [o for o in orders if o.status == 'paid']
    total_paid_amount = sum(o.amount for o in paid_orders)
    pending_count = sum(1 for o in orders if o.status == 'pending')
    paid_count = len(paid_orders)
    return render_template('admin/gift_orders.html',
        orders=orders,
        total_paid_amount=total_paid_amount,
        pending_count=pending_count,
        paid_count=paid_count,
        total_count=len(orders)
    )


@app.route('/admin/prices', methods=['GET', 'POST'])
@login_required
def admin_prices():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST' and 'price_table_image' in request.files:
        f = request.files['price_table_image']
        if f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext in SITE_IMAGE_EXT:
                name = f"price_{int(datetime.utcnow().timestamp())}{ext}"
                f.save(os.path.join(UPLOAD_PRICE_TABLE_DIR, name))
                flash('价格表图片已添加')
        return redirect(url_for('admin_prices'))
    service_type = request.args.get('service_type', '代肝')
    if service_type == '陪玩':
        prices = Price.query.filter_by(service_type='陪玩').order_by(Price.game, Price.task_type).all()
    else:
        prices = Price.query.filter(db.or_(Price.service_type == '代肝', Price.service_type.is_(None))).order_by(Price.game, Price.task_type).all()
    return render_template('admin/prices.html', prices=prices, price_table_images=list_price_table_images(), service_type=service_type)


def _parse_text_to_price_rows(text):
    """从纯文本中解析价格行，返回 [(game, task_type, price, unit), ...]。
    支持带「主线任务」「支线任务」分区的价格表，以及标题中含游戏名（如 明日方舟:终末地）。"""
    import re
    rows = []
    skip_headers = ('游戏', '价格', '任务类型', '单位', '备注', 'price list')
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    default_game = None
    current_section = None
    # 从标题行推断游戏名（如 明日方舟:终末地代肝价格表 -> 终末地）
    for ln in lines[:5]:
        if '终末地' in ln:
            default_game = '终末地'
            break
        if '明日方舟' in ln and '终末地' not in ln:
            default_game = '终末地'
            break
        for g in ('原神', '星铁', '鸣潮', '永劫无间', '三角洲'):
            if g in ln:
                default_game = g
                break
        if default_game:
            break
    for line in lines:
        if not line or len(line) < 2:
            continue
        # 分区标题：主线/支线/日常托管/探索类/基建/次要/功能/开荒
        if '主线任务' in line or (len(line) <= 8 and line.startswith('主线')):
            current_section = '主线'
            continue
        if '支线任务' in line or (len(line) <= 8 and line.startswith('支线')):
            current_section = '支线'
            continue
        if '日常托管' in line or '日常' == line[:2]:
            current_section = '日常'
            continue
        if '探索类' in line or '探索' == line[:2]:
            current_section = '探索'
            continue
        if '基建滑索' in line or '基建' in line:
            current_section = '基建'
            continue
        if '次要任务' in line or (len(line) <= 8 and '次要' in line):
            current_section = '次要'
            continue
        if '功能任务' in line or (len(line) <= 8 and '功能' in line):
            current_section = '功能'
            continue
        if '至尊开荒' in line or '开荒托管' in line:
            current_section = '开荒'
            continue
        if '二、' in line or '三、' in line or '四、' in line or '五、' in line or '六、' in line:
            if '主线' in line:
                current_section = '主线'
            elif '支线' in line:
                current_section = '支线'
            elif '日常' in line:
                current_section = '日常'
            elif '探索' in line:
                current_section = '探索'
            elif '基建' in line:
                current_section = '基建'
            elif '次要' in line:
                current_section = '次要'
            elif '功能' in line:
                current_section = '功能'
            elif '开荒' in line:
                current_section = '开荒'
            continue
        parts = re.split(r'\s+|[　\t]+', line)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            continue
        price_val = None
        rest = []
        for i in range(len(parts) - 1, -1, -1):
            s = re.sub(r'[¥￥元/rR/号图天/月]', '', parts[i]).strip()
            s = s.replace(',', '').replace('一月', '')
            try:
                price_val = float(s)
                if price_val <= 0 or price_val > 99999:
                    continue
                rest = parts[:i]
                break
            except ValueError:
                continue
        if price_val is None:
            try:
                s = re.sub(r'[¥￥元/rR/号图天/月]', '', parts[-1]).strip()
                s = s.replace(',', '').replace('一月', '')
                price_val = float(s)
                if price_val > 0 and price_val <= 99999:
                    rest = parts[:-1]
            except (ValueError, IndexError):
                continue
        if price_val is None or not rest:
            continue
        if any(h in line for h in skip_headers) and len(rest) <= 2:
            continue
        task_name = ' '.join(rest)[:50] if len(rest) > 1 else (rest[0][:50] if rest else '')
        if not task_name or not task_name.replace(' ', ''):
            continue
        if current_section:
            task_type = f'{current_section}-{task_name}'
        else:
            task_type = task_name
        game = default_game if default_game else (rest[0][:50] if rest else '未知')
        if not default_game and len(rest) >= 2:
            game, task_type = rest[0][:50], ' '.join(rest[1:])[:50]
            if current_section:
                task_type = f'{current_section}-{task_type}'
        elif not default_game and len(rest) == 1:
            game = '终末地' if '终末' in text[:200] else (rest[0][:50] if rest else '未知')
        rows.append((game, task_type, round(price_val, 2), '元/次'))
    return rows


def _ocr_image_to_text(image_path):
    """对图片做 OCR 返回文本，失败返回 None。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(image_path)
        # 若为 RGBA 转 RGB
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        text = pytesseract.image_to_string(img, lang='chi_sim+eng')
        return text or None
    except Exception:
        return None


def _parse_image_prices(image_path):
    """从图片 OCR 识别并解析出价格行。返回 (rows, error_msg)。"""
    text = _ocr_image_to_text(image_path)
    if text is None:
        try:
            import pytesseract
        except ImportError:
            return None, '请先安装: pip install pytesseract Pillow，并安装 Tesseract（含中文 chi_sim）'
        return None, 'OCR 识别失败，请检查图片是否清晰或安装 Tesseract 中文语言包'
    rows = _parse_text_to_price_rows(text)
    return rows, None


def _parse_pdf_prices(pdf_path):
    """从 PDF 中解析出 (game, task_type, price, unit) 列表。支持表格或文本行。"""
    try:
        import pdfplumber
    except ImportError:
        return None, '请先安装: pip install pdfplumber'
    rows = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table:
                        continue
                    for i, row in enumerate(table):
                        if not row or len(row) < 2:
                            continue
                        cells = [str(c).strip() if c is not None else '' for c in row]
                        cells = [c for c in cells if c]
                        if len(cells) < 2:
                            continue
                        # 跳过表头行（整行无数字或包含“游戏”“价格”等）
                        if i == 0 and all(not str(c).replace('.', '').replace('元', '').strip().isdigit() for c in cells):
                            head = ''.join(cells)
                            if '游戏' in head or '价格' in head or '任务' in head:
                                continue
                        # 找价格：最后一个数字或唯一一个数字
                        price_val = None
                        rest = []
                        for c in cells:
                            s = str(c).replace('¥', '').replace('￥', '').replace('元', '').strip()
                            try:
                                price_val = float(s)
                                rest = [x for x in cells if x != c]
                                break
                            except ValueError:
                                rest.append(c)
                        if price_val is None and len(cells) >= 3:
                            try:
                                price_val = float(str(cells[-1]).replace('¥', '').replace('￥', '').replace('元', '').strip())
                                rest = cells[:-1]
                            except (ValueError, IndexError):
                                pass
                        if price_val is None or price_val < 0:
                            continue
                        if len(rest) >= 2:
                            game, task_type = rest[0], rest[1]
                        elif len(rest) == 1:
                            game, task_type = rest[0], '默认'
                        else:
                            continue
                        if not game or not game.replace(' ', ''):
                            continue
                        unit = '元/次'
                        if len(cells) >= 4 and cells[3]:
                            unit = str(cells[3]).strip() or unit
                        rows.append((game[:50], task_type[:50], round(price_val, 2), unit[:20]))
                if not tables:
                    text = page.extract_text()
                    if text:
                        for line in text.splitlines():
                            line = line.strip()
                            parts = line.split()
                            if len(parts) >= 3:
                                try:
                                    price_val = float(parts[-1].replace('¥', '').replace('￥', ''))
                                    game, task_type = parts[0], ' '.join(parts[1:-1])
                                    if game and task_type:
                                        rows.append((game[:50], task_type[:50], round(price_val, 2), '元/次'))
                                except (ValueError, IndexError):
                                    pass
    except Exception as e:
        return None, str(e)
    return rows, None


@app.route('/admin/price/import-pdf', methods=['GET', 'POST'])
@login_required
def admin_price_import_pdf():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        if 'pdf' not in request.files:
            flash('请选择 PDF 文件')
            return redirect(url_for('admin_price_import_pdf'))
        f = request.files['pdf']
        if not f.filename or not f.filename.lower().endswith('.pdf'):
            flash('请上传 PDF 文件')
            return redirect(url_for('admin_price_import_pdf'))
        import tempfile
        path = os.path.join(tempfile.gettempdir(), secure_filename(f.filename) or 'upload.pdf')
        f.save(path)
        try:
            rows, err = _parse_pdf_prices(path)
            if err:
                flash('解析失败：' + err)
                return redirect(url_for('admin_price_import_pdf'))
            if not rows:
                flash('未能从 PDF 中解析出有效价格行，请检查表格是否为「游戏、任务类型、价格」三列')
                return redirect(url_for('admin_price_import_pdf'))
            added, updated = 0, 0
            for game, task_type, price, unit in rows:
                p = _find_platform_price_fuzzy(game, task_type)
                if p:
                    p.price = price
                    p.unit = unit or p.unit
                    updated += 1
                else:
                    db.session.add(Price(game=game.strip()[:50], task_type=task_type.strip()[:50], price=price, unit=unit or '元/次'))
                    added += 1
            db.session.commit()
            flash(f'导入成功：新增 {added} 条，更新 {updated} 条（已做模糊匹配合并）')
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return redirect(url_for('admin_prices'))
    return render_template('admin/price_import_pdf.html')


@app.route('/admin/price/import-image', methods=['GET', 'POST'])
@login_required
def admin_price_import_image():
    """上传价格表图片，OCR 识别内容后导入价格表。"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        if 'image' not in request.files:
            flash('请选择图片文件')
            return redirect(url_for('admin_price_import_image'))
        f = request.files['image']
        if not f.filename:
            flash('请选择图片文件')
            return redirect(url_for('admin_price_import_image'))
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext not in SITE_IMAGE_EXT:
            flash('请上传图片（jpg/png/gif/webp）')
            return redirect(url_for('admin_price_import_image'))
        import tempfile
        path = os.path.join(tempfile.gettempdir(), f"price_img_{int(datetime.utcnow().timestamp())}{ext}")
        f.save(path)
        try:
            rows, err = _parse_image_prices(path)
            if err:
                flash('识别失败：' + err)
                return redirect(url_for('admin_price_import_image'))
            if not rows:
                flash('未能从图片中识别出有效价格行，请确保图片清晰且包含「游戏/任务类型/价格」')
                return redirect(url_for('admin_price_import_image'))
            added, updated = 0, 0
            for game, task_type, price, unit in rows:
                p = _find_platform_price_fuzzy(game, task_type)
                if p:
                    p.price = price
                    p.unit = unit or p.unit
                    updated += 1
                else:
                    db.session.add(Price(game=game.strip()[:50], task_type=task_type.strip()[:50], price=price, unit=unit or '元/次'))
                    added += 1
            db.session.commit()
            flash(f'图片识别导入成功：识别 {len(rows)} 条，新增 {added} 条，更新 {updated} 条（已做模糊匹配合并）')
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return redirect(url_for('admin_prices'))
    return render_template('admin/price_import_image.html')


@app.route('/admin/price/edit/<int:price_id>', methods=['GET', 'POST'])
@login_required
def edit_prices(price_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    price = Price.query.get_or_404(price_id)
    if request.method == 'POST':
        old_detail = f'游戏:{price.game} 类型:{price.task_type} 价格:{price.price}'
        price.game = request.form['game']
        price.task_type = request.form['task_type']
        price.price = float(request.form['price'])
        price.unit = request.form.get('unit', '元/次')
        price.remark = request.form.get('remark', '')
        if hasattr(Price, 'service_type'):
            price.service_type = request.form.get('service_type', '代肝') or '代肝'
        log = Log(
            user_id=current_user.id,
            action='edit_price',
            target_type='price',
            target_id=price_id,
            detail=f'修改价格（ID:{price_id}）原:{old_detail} 新:游戏:{price.game} 类型:{price.task_type} 价格:{price.price}'
        )
        db.session.add(log)
        db.session.commit()
        flash('价格更新成功')
        st = request.form.get('service_type', '代肝')
        return redirect(url_for('admin_prices', service_type=st))
    return render_template('admin_price_form.html', price=price)


@app.route('/admin/price/add', methods=['GET', 'POST'])
@login_required
def admin_price_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        game = (request.form.get('game') or '').strip()
        task_type = (request.form.get('task_type') or '').strip()
        if not game or not task_type:
            flash('请填写游戏和任务类型')
            return redirect(url_for('admin_price_add'))
        try:
            price_val = float(request.form.get('price', 0))
        except (TypeError, ValueError):
            flash('请填写有效价格')
            return redirect(url_for('admin_price_add'))
        unit = (request.form.get('unit') or '元/次').strip()
        remark = (request.form.get('remark') or '').strip()
        service_type = (request.form.get('service_type') or '代肝').strip() or '代肝'
        if Price.query.filter_by(game=game, task_type=task_type, service_type=service_type).first():
            flash('该游戏+任务类型已存在，请直接编辑')
            return redirect(url_for('admin_prices', service_type=service_type))
        p = Price(game=game, task_type=task_type, price=price_val, unit=unit or '元/次', remark=remark or None, service_type=service_type)
        db.session.add(p)
        db.session.commit()
        flash('价格已添加')
        return redirect(url_for('admin_prices', service_type=service_type))
    current_service_type = request.args.get('service_type', '代肝')
    return render_template('admin_price_form.html', price=None, current_service_type=current_service_type)


@app.route('/admin/task-requests')
@login_required
def admin_task_requests():
    """管理员：打手申请新增任务类型列表，可通过/驳回。"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    requests = PendingTaskRequest.query.order_by(PendingTaskRequest.created_at.desc()).all()
    # 为每条申请计算建议平台价，供模板展示
    for r in requests:
        r.suggested_platform_price = platform_price_from_player_request(r.player_price, r.player)
    return render_template('admin/task_requests.html', requests=requests)


@app.route('/admin/task-request/<int:req_id>/approve', methods=['POST'])
@login_required
def admin_task_request_approve(req_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    req = PendingTaskRequest.query.get_or_404(req_id)
    if req.status != '待审核':
        flash('该申请已处理')
        return redirect(url_for('admin_task_requests'))
    # 按该打手当前抽成方式生成平台价（最高利润）
    platform_price = platform_price_from_player_request(req.player_price, req.player)
    if Price.query.filter_by(game=req.game, task_type=req.task_type).first():
        flash('平台已有该任务类型，已驳回重复申请')
        req.status = '已驳回'
        req.reviewed_at = datetime.utcnow()
        req.review_note = '平台已存在'
        db.session.commit()
        return redirect(url_for('admin_task_requests'))
    p = Price(game=req.game, task_type=req.task_type, price=platform_price, unit='元/次')
    db.session.add(p)
    db.session.flush()
    pp = PlayerPrice.query.filter_by(player_id=req.player_id, game=req.game, task_type=req.task_type).first()
    if pp:
        pp.price = req.player_price
    else:
        db.session.add(PlayerPrice(player_id=req.player_id, game=req.game, task_type=req.task_type, price=req.player_price))
    req.status = '已通过'
    req.reviewed_at = datetime.utcnow()
    db.session.commit()
    flash(f'已通过：{req.game} - {req.task_type}，已写入该打手报价 ￥{req.player_price:.2f}，平台价 ￥{platform_price:.2f}。您可在此修改平台价，后续按正常规则派单。')
    return redirect(url_for('edit_prices', price_id=p.id))


@app.route('/admin/task-request/<int:req_id>/reject', methods=['POST'])
@login_required
def admin_task_request_reject(req_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    req = PendingTaskRequest.query.get_or_404(req_id)
    if req.status != '待审核':
        flash('该申请已处理')
        return redirect(url_for('admin_task_requests'))
    note = (request.form.get('review_note') or '').strip()[:200]
    req.status = '已驳回'
    req.reviewed_at = datetime.utcnow()
    req.review_note = note or None
    db.session.commit()
    flash('已驳回')
    return redirect(url_for('admin_task_requests'))


@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('player_dashboard'))
    # 未登录：展示业务首页，并传入价格数据
    prices = Price.query.order_by(Price.game, Price.task_type).all()
    grouped_prices = {}
    for p in prices:
        if p.game not in grouped_prices:
            grouped_prices[p.game] = []
        grouped_prices[p.game].append(p)
    member_plans = MemberPlan.query.order_by(MemberPlan.price).all()
    return render_template('home.html', grouped_prices=grouped_prices, member_plans=member_plans)


# ---------- 全局错误页 ----------
@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='页面不存在'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500, message='服务器内部错误，请稍后再试'), 500


# ---------- 初始化数据库和默认用户 ----------
# 说明：仅做「建表 + 补列」，不删表、不删库，现有数据可保留。无需删数据表即可正常运行。
with app.app_context():
    db.create_all()
    # 为已有数据库添加 is_custom_offer 列（若不存在）
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE "order" ADD COLUMN is_custom_offer INTEGER DEFAULT 0'))
            conn.commit()
    except Exception:
        pass
    # 为已有数据库添加 service_type 列（代肝/陪玩）
    for table, col in [('"order"', 'service_type'), ('price', 'service_type'), ('player_price', 'service_type')]:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} VARCHAR(20) DEFAULT \'代肝\''))
                conn.commit()
        except Exception:
            pass
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE "order" ADD COLUMN duration_hours REAL'))
            conn.commit()
    except Exception:
        pass
    # 为已有数据库添加 balance / balance_used 列（若不存在）
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE customer ADD COLUMN balance REAL DEFAULT 0'))
            conn.commit()
    except Exception:
        pass
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE "order" ADD COLUMN balance_used REAL DEFAULT 0'))
            conn.commit()
    except Exception:
        pass
    # 二次元/无平台价意向：CustomOfferRequest.is_anime_no_display
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE custom_offer_request ADD COLUMN is_anime_no_display INTEGER DEFAULT 0'))
            conn.commit()
    except Exception:
        pass
    # 打手展示：直播间、环境照、设备照、认证
    for col_def in [
        'ALTER TABLE user ADD COLUMN live_room_url VARCHAR(500)',
        'ALTER TABLE user ADD COLUMN environment_photos TEXT',
        'ALTER TABLE user ADD COLUMN equipment_photos TEXT',
        'ALTER TABLE user ADD COLUMN equipment_desc VARCHAR(500)',
        'ALTER TABLE user ADD COLUMN is_certified INTEGER DEFAULT 0',
    ]:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(col_def))
                conn.commit()
        except Exception:
            pass
    # 为已有数据库添加礼物相关列（打手端收到的礼物页避免 OperationalError）
    try:
        from sqlalchemy import text
        t_cg = getattr(CustomerGift, '__tablename__', 'customer_gift')
        t_gp = getattr(GiftProduct, '__tablename__', 'gift_product')
        for sql in [
            f'ALTER TABLE {t_cg} ADD COLUMN message TEXT',
            f'ALTER TABLE {t_gp} ADD COLUMN is_active INTEGER DEFAULT 1',
            f'ALTER TABLE {t_gp} ADD COLUMN description TEXT',
            f'ALTER TABLE {t_gp} ADD COLUMN sort_order INTEGER DEFAULT 0',
            f'ALTER TABLE {t_gp} ADD COLUMN icon VARCHAR(30)',
            f'ALTER TABLE {t_gp} ADD COLUMN created_at DATETIME',
        ]:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception:
                pass
    except Exception:
        pass
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', password=generate_password_hash('yang86351294?'), role='admin')
        db.session.add(admin)
    # 初始化会员套餐（如果不存在）
    if not MemberPlan.query.first():
        plans = [
            MemberPlan(name='月卡会员', price=99, duration_days=30, discount=0.9, description='全店9折，优先派单', is_recommended=False),
            MemberPlan(name='季卡会员', price=259, duration_days=90, discount=0.85, description='全店85折，专属客服，每月代抽一次', is_recommended=True),
            MemberPlan(name='年卡会员', price=899, duration_days=365, discount=0.8, description='全店8折，每月代抽一次，生日福利', is_recommended=False),
            MemberPlan(name='终身会员', price=1999, duration_days=36500, discount=0.75, description='终身75折，专属客服+优先排单，限量100名', is_recommended=False),
        ]
        db.session.add_all(plans)
    # 永劫无间默认价格（无则插入，避免点击进入空页/私人定制页）
    if not Price.query.filter_by(game='永劫无间').first():
        for task_type, price in [
            ('排位代练', 15), ('日常任务', 8), ('周常', 20), ('通行证', 35), ('指定任务', 25),
        ]:
            db.session.add(Price(game='永劫无间', task_type=task_type, price=float(price), unit='元/次'))
    # 初始化虚拟礼物商品（如果不存在）
    if not GiftProduct.query.first():
        gifts = [
            GiftProduct(name='小心意', price=1, icon='fa-heart', description='聊表心意', sort_order=1),
            GiftProduct(name='鲜花', price=6, icon='fa-seedling', description='送上一束鲜花', sort_order=2),
            GiftProduct(name='奶茶', price=12, icon='fa-mug-hot', description='请你喝杯奶茶', sort_order=3),
            GiftProduct(name='大心意', price=19.9, icon='fa-star', description='感谢有你', sort_order=4),
            GiftProduct(name='超级感谢', price=66, icon='fa-gem', description='非常感谢你的付出', sort_order=5),
            GiftProduct(name='真爱暴击', price=199, icon='fa-bolt', description='真爱无敌', sort_order=6),
        ]
        db.session.add_all(gifts)
    # 注释掉的默认打手可根据需要启用
    # if not User.query.filter_by(username='ajie').first():
    #     ajie = User(username='ajie', password=generate_password_hash('123456'), role='player', player_name='阿杰')
    #     db.session.add(ajie)
    # if not User.query.filter_by(username='xiaoyue').first():
    #     xiaoyue = User(username='xiaoyue', password=generate_password_hash('123456'), role='player', player_name='小月')
    #     db.session.add(xiaoyue)
    # 客服联系信息（单行，管理端可编辑）
    if not ContactSetting.query.first():
        db.session.add(ContactSetting(wechat='1447478012', qq='1447478012', work_time='9:00-22:00'))
    db.session.commit()

@app.context_processor
def inject_pending_approval():
    if current_user.is_authenticated and current_user.role == 'admin':
        pending_count = User.query.filter_by(role='player', is_approved=False).count()
    else:
        pending_count = 0
    player_unread_count = 0
    if current_user.is_authenticated and current_user.role == 'player':
        player_unread_count = Notification.query.filter_by(
            receiver_type='player', receiver_id=current_user.id, is_read=False
        ).count()
    # 顾客手机号（后续可用 session 存储，在顾客查询订单后写入 session['customer_phone']）
    customer_phone = session.get('customer_phone')
    return dict(pending_approval_count=pending_count, player_unread_count=player_unread_count, customer_phone=customer_phone)


def site_image_url(key):
    """返回站点图片 URL，带 ?v=mtime 以便重新上传后浏览器识别新图。无上传时回退到 static/images/。"""
    filename, mtime, _ = get_site_image_info(key)
    if filename:
        return url_for('serve_site_image', key=key) + '?v=' + str(mtime)
    return url_for('static', filename=f'images/{key}.jpg')


@app.context_processor
def inject_site_image_url():
    return dict(site_image_url=site_image_url, background_slides=list_background_images())


@app.context_processor
def inject_announcement():
    """当前启用的一条公告（用于顶部公告条）。若公告表尚未创建则返回 None，避免启动报错。"""
    try:
        a = Announcement.query.filter_by(is_active=True).order_by(Announcement.sort_order.desc(), Announcement.id.desc()).first()
        return dict(current_announcement=a)
    except Exception:
        return dict(current_announcement=None)


@app.route('/game/<string:game_name>')
def game_prices(game_name):
    tasks = Price.query.filter_by(game=game_name).order_by(Price.task_type).all()
    if not tasks:
        return render_template('game_prices_empty.html', game=game_name)
    return render_template('game_prices.html', game=game_name, tasks=tasks)


# ---------- 游戏资讯（后台）---------
@app.route('/admin/news')
@login_required
def admin_news_list():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    news_list = GameNews.query.order_by(GameNews.sort_order.desc(), GameNews.created_at.desc()).all()
    return render_template('admin/news_list.html', news_list=news_list)


@app.route('/admin/news/add', methods=['GET', 'POST'])
@login_required
def admin_news_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('请填写标题')
            return redirect(url_for('admin_news_add'))
        summary = (request.form.get('summary') or '').strip()[:500]
        content = request.form.get('content') or ''
        game = (request.form.get('game') or '').strip()[:50]
        is_published = request.form.get('is_published') == '1'
        try:
            sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            sort_order = 0
        cover = None
        if 'cover' in request.files:
            f = request.files['cover']
            if f.filename:
                cover = secure_filename(f"news_{int(datetime.utcnow().timestamp())}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], cover))
        news = GameNews(title=title, summary=summary, content=content, game=game or None, is_published=is_published, sort_order=sort_order, cover=cover)
        db.session.add(news)
        db.session.commit()
        flash('资讯已添加')
        return redirect(url_for('admin_news_list'))
    games = db.session.query(Price.game).distinct().all()
    return render_template('admin/news_form.html', news=None, games=games)


@app.route('/admin/news/edit/<int:news_id>', methods=['GET', 'POST'])
@login_required
def admin_news_edit(news_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    news = GameNews.query.get_or_404(news_id)
    if request.method == 'POST':
        news.title = (request.form.get('title') or '').strip() or news.title
        news.summary = (request.form.get('summary') or '').strip()[:500]
        news.content = request.form.get('content') or ''
        news.game = (request.form.get('game') or '').strip()[:50] or None
        news.is_published = request.form.get('is_published') == '1'
        try:
            news.sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            pass
        if 'cover' in request.files:
            f = request.files['cover']
            if f.filename:
                news.cover = secure_filename(f"news_{int(datetime.utcnow().timestamp())}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], news.cover))
        db.session.commit()
        flash('资讯已更新')
        return redirect(url_for('admin_news_list'))
    games = db.session.query(Price.game).distinct().all()
    return render_template('admin/news_form.html', news=news, games=games)


@app.route('/admin/news/delete/<int:news_id>', methods=['POST'])
@login_required
def admin_news_delete(news_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    news = GameNews.query.get_or_404(news_id)
    db.session.delete(news)
    db.session.commit()
    flash('资讯已删除')
    return redirect(url_for('admin_news_list'))


# ---------- 站内公告 ----------
@app.route('/admin/announcements')
@login_required
def admin_announcements():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    items = Announcement.query.order_by(Announcement.sort_order.desc(), Announcement.created_at.desc()).all()
    return render_template('admin/announcements.html', items=items)


@app.route('/admin/announcement/add', methods=['GET', 'POST'])
@login_required
def admin_announcement_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('请填写标题')
            return redirect(url_for('admin_announcement_add'))
        content = (request.form.get('content') or '').strip()
        link_url = (request.form.get('link_url') or '').strip() or None
        is_active = request.form.get('is_active') == '1'
        try:
            sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            sort_order = 0
        a = Announcement(title=title, content=content, link_url=link_url, is_active=is_active, sort_order=sort_order)
        db.session.add(a)
        db.session.commit()
        flash('公告已添加')
        return redirect(url_for('admin_announcements'))
    return render_template('admin/announcement_form.html', item=None)


@app.route('/admin/announcement/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def admin_announcement_edit(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    item = Announcement.query.get_or_404(item_id)
    if request.method == 'POST':
        item.title = (request.form.get('title') or '').strip() or item.title
        item.content = (request.form.get('content') or '').strip()
        item.link_url = (request.form.get('link_url') or '').strip() or None
        item.is_active = request.form.get('is_active') == '1'
        try:
            item.sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            pass
        db.session.commit()
        flash('公告已更新')
        return redirect(url_for('admin_announcements'))
    return render_template('admin/announcement_form.html', item=item)


@app.route('/admin/announcement/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_announcement_delete(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    item = Announcement.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash('公告已删除')
    return redirect(url_for('admin_announcements'))


# ---------- 常见问题 FAQ ----------
@app.route('/faq')
def faq_list():
    items = Faq.query.order_by(Faq.sort_order, Faq.id).all()
    return render_template('faq.html', items=items)


@app.route('/admin/faq')
@login_required
def admin_faq_list():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    items = Faq.query.order_by(Faq.sort_order, Faq.id).all()
    return render_template('admin/faq_list.html', items=items)


@app.route('/admin/faq/add', methods=['GET', 'POST'])
@login_required
def admin_faq_add():
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        question = (request.form.get('question') or '').strip()
        answer = (request.form.get('answer') or '').strip()
        if not question or not answer:
            flash('请填写问题和答案')
            return redirect(url_for('admin_faq_add'))
        try:
            sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            sort_order = 0
        db.session.add(Faq(question=question, answer=answer, sort_order=sort_order))
        db.session.commit()
        flash('FAQ 已添加')
        return redirect(url_for('admin_faq_list'))
    return render_template('admin/faq_form.html', item=None)


@app.route('/admin/faq/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def admin_faq_edit(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    item = Faq.query.get_or_404(item_id)
    if request.method == 'POST':
        item.question = (request.form.get('question') or '').strip() or item.question
        item.answer = (request.form.get('answer') or '').strip() or item.answer
        try:
            item.sort_order = int(request.form.get('sort_order', 0))
        except (TypeError, ValueError):
            pass
        db.session.commit()
        flash('FAQ 已更新')
        return redirect(url_for('admin_faq_list'))
    return render_template('admin/faq_form.html', item=item)


@app.route('/admin/faq/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_faq_delete(item_id):
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    item = Faq.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash('FAQ 已删除')
    return redirect(url_for('admin_faq_list'))


@app.route('/admin/site_images', methods=['GET', 'POST'])
@login_required
def admin_site_images():
    """管理端站点图片设置：上传/替换背景图与支付码图；背景轮播为图床式多图上传。"""
    if current_user.role != 'admin':
        return redirect(url_for('player_dashboard'))
    if request.method == 'POST':
        # 删除某张背景轮播图
        delete_bg = request.form.get('delete_bg')
        if delete_bg:
            safe_name = os.path.basename(secure_filename(delete_bg))
            if safe_name:
                path = os.path.join(UPLOAD_BG_DIR, safe_name)
                if os.path.isfile(path) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(UPLOAD_BG_DIR):
                    try:
                        os.remove(path)
                        flash('已删除该背景图')
                    except OSError:
                        flash('删除失败', 'error')
            return redirect(url_for('admin_site_images'))
        # 上传一张新的背景轮播图（图床追加）
        bg_file = request.files.get('bg_upload')
        if bg_file and bg_file.filename:
            ext = os.path.splitext(secure_filename(bg_file.filename))[1].lower() or '.jpg'
            if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                new_name = f"bg_{int(datetime.utcnow().timestamp())}_{secure_filename(bg_file.filename)}"
                bg_file.save(os.path.join(UPLOAD_BG_DIR, new_name))
                flash('已添加一张背景轮播图')
            return redirect(url_for('admin_site_images'))
        # 原有：站点固定 key 图片上传
        for key in SITE_IMAGE_KEYS:
            if key not in request.files:
                continue
            f = request.files.get(key)
            if not f or not f.filename:
                continue
            ext = os.path.splitext(secure_filename(f.filename))[1] or '.jpg'
            if ext.lower() not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                continue
            try:
                for old in os.listdir(SITE_IMAGES_DIR):
                    if old.startswith(key + '.'):
                        try:
                            os.remove(os.path.join(SITE_IMAGES_DIR, old))
                        except OSError:
                            pass
                        break
            except OSError:
                pass
            new_name = key + ext
            f.save(os.path.join(SITE_IMAGES_DIR, new_name))
        flash('站点图片已更新，刷新前台页面即可看到新图。')
        return redirect(url_for('admin_site_images'))
    # GET: 展示当前图片与上传表单
    image_info = {k: get_site_image_info(k) for k in SITE_IMAGE_KEYS}
    background_slides = list_background_images()
    return render_template('admin/site_images.html', image_info=image_info, site_image_keys=SITE_IMAGE_KEYS, site_image_url=site_image_url, background_slides=background_slides)


if __name__ == '__main__':
    app.run(debug=True)
