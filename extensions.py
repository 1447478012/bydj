# extensions.py

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# 创建 db 和 login_manager 对象
db = SQLAlchemy()
login_manager = LoginManager()