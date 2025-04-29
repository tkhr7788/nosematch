from flask import Flask, render_template, request, redirect, url_for, session
from models import db, User, Event, RSVP, Plan
from datetime import datetime
import requests
import urllib.parse
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import random 
import requests 
from math import radians, sin, cos, atan2, sqrt
from math import hypot



app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///noritomo.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "supersecretkey"
db.init_app(app)

# ここにあなたのAPIキーを貼り付け！
GOOGLE_MAPS_API_KEY = "AIzaSyAsWa07NpEhvqW7pzbTR8LKLJxXL8YxnrE"

def geo_distance(a, b):
    """2 点間の直近似距離 (km)"""
    # a, b は (lat, lng) タプル
    return hypot(a[0]-b[0], a[1]-b[1]) * 111  # ざっくり 1° ≒ 111 km

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
    # 緯度・経度が両方存在する人だけを対象にする
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
def haversine(p1, p2):
    """2点 (lat, lng) の球面距離(km)"""
    R = 6371
    lat1, lon1 = map(radians, p1)
    lat2, lon2 = map(radians, p2)
    dlat, dlon = lat2-lat1, lon2-lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2*R*atan2(sqrt(a), sqrt(1-a))

def assign_carpool_balance(rsvps, direction):
    """
    5km 以内を優先しつつ、残りはドライバー負担が均等になるように割り振る
    戻り値: (carpool_dict, missed_list)
    """
    # ------------------------ データ整形 ------------------------
    drivers = [r for r in rsvps if getattr(r, f"{direction}_capacity") > 0]
    children = [r for r in rsvps]   # 子ども単位でなく RSVP 単位で扱う

    # 各ドライバーの空席と load
    avail = {d.name: getattr(d, f"{direction}_capacity") for d in drivers}
    load  = {d.name: 0 for d in drivers}
    loc   = {r.name: (r.lat, r.lng) for r in rsvps}

    carpool = {d.name: [] for d in drivers}
    missed  = []

    # ------------------- フェーズ① : 近距離優先 -------------------
    for kid in children:
        chosen = None
        best_d = 1e9
        for d in drivers:
            if avail[d.name] == 0 or None in loc[kid.name] or None in loc[d.name]:
                continue
            dist = geo_distance(loc[kid.name], loc[d.name])
            if dist <= 5:                              # 5 km 圏内
                score = (load[d.name], dist)           # まず load, その次距離
                if score < (load.get(chosen, 1e9), best_d):
                    chosen, best_d = d.name, dist
        if chosen:
            carpool[chosen].append(kid.name)
            load[chosen]  += 1
            avail[chosen] -= 1
        else:
            missed.append(kid)        # 後回し

    # --------------- フェーズ② : 残りをバランス割当て ---------------
    for kid in missed[:]:
        # 空席があるドライバーのみ対象
        cand = [d for d in drivers if avail[d.name] > 0 and None not in loc[d.name]]
        if not cand:
            break
        # load が最小 → 距離が最短
        cand.sort(key=lambda d: (load[d.name], geo_distance(loc[kid.name], loc[d.name])))
        chosen = cand[0].name
        carpool[chosen].append(kid.name)
        load[chosen]  += 1
        avail[chosen] -= 1
        missed.remove(kid)

    return carpool, missed

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
        
        # ここでチェック追加！
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

def calculate_center(points):
    lat_sum = sum(p[0] for p in points)
    lng_sum = sum(p[1] for p in points)
    n = len(points)
    return (lat_sum / n, lng_sum / n)

def reverse_geocode(lat, lng):
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lng}"
    headers = {"User-Agent": "noritomo/1.0"}
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

@app.post("/api/plan/<int:eid>")
def generate_plan(eid):
    ev = Event.query.get_or_404(eid)
    rsvps = RSVP.query.filter_by(event_id=eid).all()

    # ここで行き（go）と帰り（back）を分けてループする
    for d in ('go', 'back'):
        # 配車を決める
        carpool, missed = assign_carpool_balance(rsvps, d)
        if missed:
            print(f"[WARN] {len(missed)}人の乗車割り当てに失敗しました")

        # 中間地点を求めるために、各車の子供たちの緯度・経度を集める
        rsvp_dict = {r.name: (r.lat, r.lng) for r in rsvps}

        route_text = ""
        for driver, kids in carpool.items():
            # ドライバー自身の位置情報から地域名を取得する
            if driver in rsvp_dict:
                area_name = reverse_geocode(*rsvp_dict[driver])
            else:
                area_name = "地域不明"

            route_text += f"{driver}の車（{area_name}）：{', '.join(kids)}\n"

        # 行き/帰りそれぞれ保存
        Plan.query.filter_by(event_id=eid, direction=d).delete()
        db.session.add(Plan(event_id=eid, direction=d, body=route_text))

    db.session.commit()

    return {"ok": True}

# ---------- 配車プラン一覧 ----------
@app.route("/plans")
def plan_list():
    events = Event.query.order_by(Event.date.desc()).all()
    plans  = Plan.query.order_by(Plan.id).all()

    # id → title の辞書を作る
    event_titles = {e.id: e.title for e in events}

    # 右上に出す現在時刻
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d (%a) %H:%M")

    return render_template(
        "plans.html",
        events=events,
        plans=plans,
        event_titles=event_titles,
        now_str=now_str,
    )

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
