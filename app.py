from flask import Flask, render_template, request, redirect, url_for, session
from models import db, User, Event, RSVP, Plan
from datetime import datetime
import requests
import urllib.parse
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///noritomo.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "supersecretkey"
db.init_app(app)

def geocode_address(address):
    base_url = "https://nominatim.openstreetmap.org/search?"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
    }
    headers = {"User-Agent": "noritomo/1.0"}
    url = base_url + urllib.parse.urlencode(params)
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()
        data = res.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print("Geocode failed:", e)
    return None, None

def make_route(rsvps, spot_lat, spot_lng):
    points = [(r.lat, r.lng) for r in rsvps if r.lat and r.lng]
    points.append((spot_lat, spot_lng))

    def calc_dist(p1, p2):
        return ((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)**0.5 * 111

    dist_matrix = [[calc_dist(p1, p2) for p2 in points] for p1 in points]

    n = len(points)
    manager = pywrapcp.RoutingIndexManager(n, 1, n-1)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return int(dist_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)] * 1000)

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_parameters)

    if solution:
        order = []
        index = routing.Start(0)
        while not routing.IsEnd(index):
            order.append(manager.IndexToNode(index))
            index = solution.Value(routing.NextVar(index))
        return order
    else:
        return []

# ---------- ログイン・新規登録 ----------
@app.route("/")
def root():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username, password=password).first()
        if user:
            session["user_id"] = user.id
            session["username"] = user.username
            session["role"] = user.role
            return redirect(url_for("event_list"))
        else:
            return render_template("login.html", error="ログイン失敗。IDまたはパスワードが違います。")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="このIDは既に使われています。")
        user = User(username=username, password=password, role="user")
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- 配車システム本体 ----------
@app.route("/events")
def event_list():
    events = Event.query.order_by(Event.date.desc()).all()
    return render_template("event_list.html", events=events)

@app.route("/events/new", methods=["GET", "POST"])
def new_event():
    if request.method == "POST":
        e = Event(
            title=request.form["title"],
            date=datetime.fromisoformat(request.form["date"]),
            spot=request.form["spot"],
        )
        db.session.add(e)
        db.session.commit()
        return redirect(url_for("event_list"))
    return render_template("new.html")

@app.route("/events/<int:eid>", methods=["GET", "POST"])
def answer(eid):
    ev = Event.query.get_or_404(eid)
    if request.method == "POST":
        lat, lng = geocode_address(request.form["address"])
        r = RSVP(
            event_id=eid,
            name=request.form["name"],
            address=request.form["address"],
            lat=lat,
            lng=lng,
            children=request.form["children"],
            child_cnt=int(request.form["child_cnt"]),
            capacity=int(request.form["capacity"]),
            go_ok=bool(request.form.get("go_ok")),
            back_ok=bool(request.form.get("back_ok")),
            pickup_only=bool(request.form.get("pickup_only")),
        )
        db.session.add(r)
        db.session.commit()
        return redirect(url_for("thanks", eid=eid))
    rsvps = RSVP.query.filter_by(event_id=eid).all()
    return render_template("answer.html", ev=ev, rsvps=rsvps)

@app.route("/events/<int:eid>/thanks")
def thanks(eid):
    return render_template("thanks.html", eid=eid)

@app.route("/events/<int:eid>/admin")
def admin(eid):
    ev = Event.query.get_or_404(eid)
    rsvps = RSVP.query.filter_by(event_id=eid).all()
    plans = Plan.query.filter_by(event_id=eid).all()
    return render_template("admin.html", ev=ev, rsvps=rsvps, plans=plans)

@app.post("/api/plan/<int:eid>")
def generate_plan(eid):
    ev = Event.query.get_or_404(eid)
    rsvps = RSVP.query.filter_by(event_id=eid).all()

    spot_lat, spot_lng = geocode_address(ev.spot)

    order = make_route(rsvps, spot_lat, spot_lng)

    names = [r.name for r in rsvps]
    names.append("集合スポット")

    route_text = " → ".join([names[i] for i in order])

    for d in ("go", "back"):
        Plan.query.filter_by(event_id=eid, direction=d).delete()
        db.session.add(Plan(event_id=eid, direction=d, body=route_text))
    db.session.commit()
    return {"ok": True}

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
