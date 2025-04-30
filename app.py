from flask import Flask, render_template, request, redirect, url_for, session
from models import db, User, Event, RSVP, Plan
from datetime import datetime
import requests
import urllib.parse
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import random 
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///noritomo.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "supersecretkey"
db.init_app(app)

# ここにあなたのAPIキーを貼り付け！
GOOGLE_MAPS_API_KEY = ""

def geo_distance(a, b):
    """2 点間の直近似距離 (km)"""
    # a, b は (lat, lng) タプル
    return hypot(a[0]-b[0], a[1]-b[1]) * 111  # ざっくり 1° ≒ 111 km

# ----------------------- 便利関数 -----------------------
def geocode_address(address):
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(address)}&key={GOOGLE_MAPS_API_KEY}"
    try:
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        data = res.json()
        if data['status'] == 'OK':
            location = data['results'][0]['geometry']['location']
            return location['lat'], location['lng']
    except Exception as e:
        print("Google Geocode failed:", e)
    return None, None

def make_route(rsvps, spot_lat, spot_lng):
    valid_rsvps = [r for r in rsvps if r.lat is not None and r.lng is not None]
    points = [(r.lat, r.lng) for r in valid_rsvps]
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

def make_carpool(rsvps):
    drivers = [r for r in rsvps if r.lat is not None and r.lng is not None and r.go_capacity > 0]
    passengers = [r for r in rsvps if r.lat is not None and r.lng is not None]
    random.shuffle(passengers)

    if not drivers:
        return "車を出せる人がいません。"

    drivers.sort(key=lambda r: (r.lat, r.lng))

    car_assignments = {}
    driver_index = 0
    for p in passengers:
        while True:
            driver = drivers[driver_index % len(drivers)]
            if driver.name not in car_assignments:
                car_assignments[driver.name] = []
            if len(car_assignments[driver.name]) < driver.go_capacity:
                car_assignments[driver.name].append(p.name)
                break
            else:
                driver_index += 1

    return car_assignments

def reverse_geocode(lat, lng):
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lng}"
    headers = {"User-Agent": "nosematch/1.0"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        city = data.get('address', {}).get('city', '')
        town = data.get('address', {}).get('suburb', '')
        return f"{city}{town}周辺"
    except Exception as e:
        print("Reverse geocoding failed:", e)
        return "地域不明"

# ----------------------- ログイン・登録機能 -----------------------
@app.route("/")
def root():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):  # セキュアな照合
            session["user_id"] = user.id
            session["username"] = user.username
            session["role"] = user.role

            if user.role == "admin":
                return redirect(url_for("admin_top"))
            else:
                return redirect(url_for("user_top"))
        else:
            return render_template("login.html", error="ログイン失敗。IDまたはパスワードが違います。")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        role = request.form["role"]
        invite_code = request.form.get("invite_code")

        if role == "admin":
            if invite_code != "ABC123":
                return render_template("register.html", error="招待コードが間違っています。")

        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="このIDは既に使われています。")

        hashed_password = generate_password_hash(password)  # ハッシュ化
        user = User(username=username, password=hashed_password, role=role)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# 🔥 追加：管理者TOP
@app.route("/admin")
def admin_top():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))
    username = session.get("username")
    return render_template("admin.html", username=username)

# 🔥 追加：一般ユーザーTOP
@app.route("/user_top")
def user_top():
    if "user_id" not in session or session.get("role") != "user":
        return redirect(url_for("login"))
    username = session.get("username")
    return render_template("user_top.html", username=username)

# ----------------------- 配車システム本体 -----------------------
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
        if lat is None or lng is None:
            return render_template("answer.html", ev=ev, rsvps=RSVP.query.filter_by(event_id=eid).all(), error="住所が正しく認識できませんでした。もう一度確認してください。")

        r = RSVP(
            event_id=eid,
            name=request.form["name"],
            address=request.form["address"],
            lat=lat,
            lng=lng,
            children=request.form["children"],
            child_cnt=int(request.form["child_cnt"]),
            go_capacity=int(request.form["go_capacity"]),
            back_capacity=int(request.form["back_capacity"]),
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

    # RSVPデータを辞書化（位置情報用と、全体参照用の2つ）
    rsvp_dict_coords = {r.name: (r.lat, r.lng) for r in rsvps}
    rsvp_dict_full = {r.name: r for r in rsvps}

    for d in ('go', 'back'):
        # 配車を決める
        carpool, missed = assign_carpool_balance(rsvps, d)
        if missed:
            print(f"[WARN] {len(missed)}人の乗車割り当てに失敗しました")

        route_text = ""
        for driver, kid_names in carpool.items():
            # ドライバーの地域名を取得
            if driver in rsvp_dict_coords:
                area_name = reverse_geocode(*rsvp_dict_coords[driver])
            else:
                area_name = "地域不明"

            # 各kidのchildren（子どもの名前）をまとめる
            child_names = []
            for name in kid_names:
                rsvp = rsvp_dict_full.get(name)
                if rsvp:
                    # 「子どもA1, 子どもA2」のような文字列をリスト化
                    child_names.extend([c.strip() for c in rsvp.children.split(",")])
                else:
                    child_names.append(name)  # 念のため fallback

            route_text += f"{driver}の車（{area_name}）：{', '.join(child_names)}\n"

    for d in ("go", "back"):
        Plan.query.filter_by(event_id=eid, direction=d).delete()
        db.session.add(Plan(event_id=eid, direction=d, body=route_text))
    db.session.commit()
    return {"ok": True}

# ---------- 配車プラン一覧 ----------
@app.route("/plans")
def plan_list():
    events = Event.query.order_by(Event.date.desc()).all()
    plans = Plan.query.all()
    return render_template("plans.html", events=events, plans=plans)

@app.route("/rsvp/<int:id>/edit", methods=["GET", "POST"])
def edit_rsvp(id):
    if session.get("role") != "admin":
        return "権限がありません", 403  # 管理者じゃなかったら禁止
    r = RSVP.query.get_or_404(id)
    if request.method == "POST":
        r.name = request.form["name"]
        r.children = request.form["children"]
        r.address = request.form["address"]
        r.child_cnt = int(request.form["child_cnt"])
        r.go_capacity = int(request.form["go_capacity"])
        r.back_capacity = int(request.form["back_capacity"])
        r.lat, r.lng = geocode_address(r.address)
        db.session.commit()
        return redirect(url_for("admin", eid=r.event_id))
    return render_template("edit_rsvp.html", rsvp=r)


@app.route("/rsvp/<int:id>/delete", methods=["POST"])
def delete_rsvp(id):
    if session.get("role") != "admin":
        return "権限がありません", 403  # 管理者じゃなかったら禁止
    r = RSVP.query.get_or_404(id)
    eid = r.event_id
    db.session.delete(r)
    db.session.commit()
    return redirect(url_for("admin", eid=eid))



if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
