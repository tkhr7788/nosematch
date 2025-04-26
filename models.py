from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String)
    date  = db.Column(db.DateTime)
    spot  = db.Column(db.String)

class RSVP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id  = db.Column(db.Integer, db.ForeignKey("event.id"))
    name      = db.Column(db.String)    # 保護者名
    address   = db.Column(db.String)    # 住所
    lat       = db.Column(db.Float)     # 緯度
    lng       = db.Column(db.Float)     # 経度
    children  = db.Column(db.String)    # ★ 子ども名前（カンマ区切り）
    child_cnt = db.Column(db.Integer)   # 子ども人数
    capacity  = db.Column(db.Integer)   # 車の定員
    go_ok     = db.Column(db.Boolean)   # 行き車出せるか
    back_ok   = db.Column(db.Boolean)   # 帰り車出せるか
    pickup_only = db.Column(db.Boolean) # 迎えのみOK

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id  = db.Column(db.Integer, db.ForeignKey("event.id"))
    direction = db.Column(db.String)  # "go" / "back"
    body      = db.Column(db.Text)
