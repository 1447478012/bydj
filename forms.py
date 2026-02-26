from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length


class PlayerEditForm(FlaskForm):
    player_name = StringField('打手姓名', validators=[DataRequired()])
    phone = StringField('手机号')
    wechat = StringField('微信')
    income_mode = SelectField('收益模式', choices=[
        ('fixed', '固定底价'),
        ('custom', '自主报价'),
        ('tiered', '阶梯抽成'),
    ], validators=[DataRequired()])
    tiered_rates = TextAreaField('阶梯规则 (JSON格式)')
    allow_custom_price = BooleanField('允许自主报价')
    is_certified = BooleanField('打手认证（展示页显示认证标识）')
    submit = SubmitField('保存')

class LoginForm(FlaskForm):
    username = StringField('用户名', validators=[DataRequired()])
    password = PasswordField('密码', validators=[DataRequired()])

class OrderForm(FlaskForm):
    order_no = StringField('订单号', validators=[DataRequired()])
    game = SelectField('游戏', choices=[
    ('原神','原神'),
    ('鸣潮','鸣潮'),
    ('星铁','星铁'),
    ('三角洲','三角洲'),
    ('终末地','终末地'),
    ('永劫无间','永劫无间'),
])
    task_type = StringField('任务类型', validators=[DataRequired()])
    customer_price = FloatField('客户价', validators=[DataRequired()])
    player_price = FloatField('打手底价', validators=[DataRequired()])
    player_id = SelectField('分配打手', coerce=int)  # 选项将在视图中动态添加
    notes = TextAreaField('备注')

class FeedbackForm(FlaskForm):
    title = StringField('标题', validators=[DataRequired(), Length(1, 100)])
    content = TextAreaField('内容', validators=[DataRequired(), Length(1, 500)])
    submit = SubmitField('提交反馈')
