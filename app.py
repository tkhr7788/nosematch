from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from models import db, User, Event, RSVP, Plan
from datetime import datetime
import requests
import urllib.parse
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
import random 
from werkzeug.security import generate_password_hash, check_password_hash
import os
from math import hypot 


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///noritomo.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "supersecretkey"
db.init_app(app)

def geo_distance(a, b):
    """2 点間の直近似距離 (km)"""
    # a, b は (lat, lng) タプル
    return hypot(a[0]-b[0], a[1]-b[1]) * 111  # ざっくり 1° ≒ 111 km

# ----------------------- 便利関数 -----------------------
# 定数としてキーをここに直接書く
GOOGLE_MAPS_API_KEY = "AIzaSyAsWa07NpEhvqW7pzbTR8LKLJxXL8YxnrE"

def geocode_address(address):
    """Google Maps APIを使う。キーが無ければNominatimにフォールバック。"""

    if GOOGLE_MAPS_API_KEY:
        print("[INFO] Google API を使用します")
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(address)}&key={GOOGLE_MAPS_API_KEY}"
        try:
            res = requests.get(url, timeout=5)
            res.raise_for_status()
            data = res.json()
            if data['status'] == 'OK':
                loc = data['results'][0]['geometry']['location']
                return loc['lat'], loc['lng']
        except Exception as e:
            print("[Google] Geocode failed:", e)

    # フォールバック（Googleキーが無い or エラー）
    print("[INFO] Nominatim (無料API) を使用します")
    url = f"https://nominatim.openstreetmap.org/search?format=json&q={urllib.parse.quote(address)}"
    headers = {"User-Agent": "nosematch/1.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()
        results = res.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print("[Nominatim] Geocode failed:", e)

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

def assign_carpool_balance(rsvps, direction):
    """
    ① 3 km 以内なら同じドライバーへ優先乗車
    ② 残りは各ドライバーの load を均等化
    ③ 距離が近い順に割り振り
    戻り値: (carpool_dict, missed_list)
    """
    drivers = [r for r in rsvps if getattr(r, f"{direction}_capacity") > 0]
    children = [r for r in rsvps]              # RSVP 単位で扱う
    avail = {d.name: getattr(d, f"{direction}_capacity") for d in drivers}
    load  = {d.name: 0 for d in drivers}
    loc   = {r.name: (r.lat, r.lng) for r in rsvps}
    carpool, missed = {d.name: [] for d in drivers}, []

    # ── Phase-1: 半径 3 km 以内は同乗優先 ──────────────────
    for kid in children:
        chosen, best_d = None, 1e9
        for d in drivers:
            if avail[d.name] == 0 or None in loc[kid.name] or None in loc[d.name]:
                continue
            dist = geo_distance(loc[kid.name], loc[d.name])
            if dist <= 3 and (load[d.name], dist) < (load.get(chosen, 1e9), best_d):
                chosen, best_d = d.name, dist
        if chosen:
            carpool[chosen].append(kid.name); load[chosen] += 1; avail[chosen] -= 1
        else:
            missed.append(kid)

    # ── Phase-2: 残りを負担均等＆距離最短で割当て ───────────────
    for kid in missed[:]:
        cand = [d for d in drivers if avail[d.name] > 0 and None not in loc[d.name]]
        if not cand: break
        cand.sort(key=lambda d: (load[d.name], geo_distance(loc[kid.name], loc[d.name])))
        chosen = cand[0].name
        carpool[chosen].append(kid.name); load[chosen] += 1; avail[chosen] -= 1; missed.remove(kid)

    return carpool, missed

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

@app.route("/admin/events")
def admin_events():
    events = Event.query.order_by(Event.date.desc()).all()
    return render_template("admin_event_list.html", events=events)

@app.route("/events/new", methods=["GET", "POST"])
def new_event():
    if request.method == "POST":
        # 日付と時間を取得
        date_str = request.form["date"]
        hour = request.form["hour"]
        minute = request.form["minute"]

        # 開始日時の文字列を組み立ててdatetimeに変換
        datetime_str = f"{date_str}T{hour}:{minute}"
        date_obj = datetime.fromisoformat(datetime_str)

        e = Event(
            title=request.form["title"],
            date=date_obj,
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

@app.route("/events/<int:eid>/delete", methods=["POST"])
def delete_event(eid):
    if session.get("role") != "admin":
        return "権限がありません", 403
    ev = Event.query.get_or_404(eid)

    # 関連データも削除（任意）
    RSVP.query.filter_by(event_id=eid).delete()
    Plan.query.filter_by(event_id=eid).delete()

    db.session.delete(ev)
    db.session.commit()
    return redirect(url_for("admin_events"))  # 一覧へ戻る


@app.route("/events/<int:eid>/manage")
def event_manage(eid):
    ev = Event.query.get_or_404(eid)
    rsvps = RSVP.query.filter_by(event_id=eid).all()
    return render_template("event_manage.html", ev=ev, rsvps=rsvps)

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
    missed_total = 0 

    # RSVPデータを辞書化（位置情報用と、全体参照用の2つ）
    rsvp_dict_coords = {r.name: (r.lat, r.lng) for r in rsvps}
    rsvp_dict_full = {r.name: r for r in rsvps}

    # "go" / "back" ごとに route_text を記録しておく
    route_texts = {}

    for d in ('go', 'back'):
        # 配車を決める
        carpool, missed = assign_carpool_balance(rsvps, d)
        if missed:
            print(f"[WARN] {len(missed)}人の乗車割り当てに失敗しました")
            missed_total += len(missed)

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

        route_texts[d] = route_text  # ← go/back それぞれ保存

    # DB 保存処理
    for d in ("go", "back"):
        Plan.query.filter_by(event_id=eid, direction=d).delete()
        db.session.add(Plan(event_id=eid, direction=d, body=route_texts.get(d, "")))

    db.session.commit()

    return jsonify(ok=True, missed=missed_total)  # JSONで返すだけ


# ---------- 配車プラン一覧 ----------
@app.route("/plans")
def plan_list():
    events = Event.query.order_by(Event.date.desc()).all()
    plans  = Plan.query.all()

    # event_id → title だけの辞書を作る
    event_titles = {e.id: e.title for e in events}

    return render_template("plans.html",
                           events=events,
                           plans=plans,
                           event_titles=event_titles)   # 追加


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

# ---------- 管理者：履歴 ----------
@app.route("/admin/history")
def history():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))
    return "<h1>履歴ページ（準備中）</h1>"

def init_db():
    with app.app_context():
        db.create_all()

        # 初期ユーザーが存在しなければ追加
        if not User.query.filter_by(username="admin").first():
            admin = User(
                username="admin",
                password=generate_password_hash("admin123"),  # 実運用ではハッシュを使う
                role="admin"
            )
            db.session.add(admin)
            db.session.commit()
            print("初期ユーザー admin を追加しました")
        else:
            print("初期ユーザー admin はすでに存在します")

if __name__ == "__main__":
    init_db()  # ← ここでテーブル作成＆admin登録

    port = int(os.environ.get("PORT", 5000))
    try:
        app.run(host="0.0.0.0", port=port, debug=True)
    except OSError as e:
        if e.errno == 98:  # Address already in use
            app.run(host="0.0.0.0", port=5001, debug=True)
        else:
            raise



