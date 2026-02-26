# routes.py

from app import app
from flask import render_template, redirect, url_for, request
from flask_login import login_user, login_required, logout_user, current_user
from models import User, Order
from extensions import db

# 首页路由
@app.route('/')
def home():
    # 如果用户已经登录，重定向到仪表盘
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    # 如果未登录，重定向到登录页面
    return redirect(url_for('login'))

# 登录视图
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # 这里添加密码验证逻辑
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:  # 这里可以使用 hashed password 进行验证
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return "登录失败！用户名或密码错误"
    return render_template('login.html')

# 登出视图
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# 仪表盘视图（管理员和打手）
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'admin':
        orders = Order.query.all()  # 管理员查看所有订单
    else:
        orders = Order.query.filter_by(player_id=current_user.id)  # 打手只看自己分配的订单
    return render_template('dashboard.html', orders=orders)

# 添加订单视图（管理员）
@app.route('/add_order', methods=['GET', 'POST'])
@login_required
def add_order():
    if current_user.role != 'admin':
        return "没有权限访问"
    
    if request.method == 'POST':
        order_number = request.form['order_number']
        game = request.form['game']
        task_type = request.form['task_type']
        client_price = request.form['client_price']
        player_price = request.form['player_price']
        status = request.form['status']
        remarks = request.form['remarks']
        player_id = request.form['player_id']
        
        order = Order(order_number=order_number, game=game, task_type=task_type,
                      client_price=client_price, player_price=player_price,
                      status=status, remarks=remarks, player_id=player_id)
        db.session.add(order)
        db.session.commit()
        return redirect(url_for('dashboard'))
    
    players = User.query.filter_by(role='player').all()  # 获取所有打手
    return render_template('add_order.html', players=players)
