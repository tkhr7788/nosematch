from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String, unique=True, nullable=False)
    password = db.Column(db.String, nullable=False)
    role = db.Column(db.String, default="user")  # "admin" or "user"

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String)
    date  = db.Column(db.DateTime)
    spot  = db.Column(db.String)

class RSVP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id  = db.Column(db.Integer, db.ForeignKey("event.id"))
    name      = db.Column(db.String)
    address   = db.Column(db.String)
    lat       = db.Column(db.Float)
    lng       = db.Column(db.Float)
    children  = db.Column(db.String)
    child_cnt = db.Column(db.Integer)
    capacity  = db.Column(db.Integer)
    go_ok     = db.Column(db.Boolean)
    back_ok   = db.Column(db.Boolean)
    pickup_only = db.Column(db.Boolean)

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id  = db.Column(db.Integer, db.ForeignKey("event.id"))
    direction = db.Column(db.String)
    body      = db.Column(db.Text)
