"""띵지고 Firebase(공용) 버전. 배포용 실행 파일입니다."""

from __future__ import annotations
import html
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import firebase_admin
from firebase_admin import credentials, firestore
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="띵지고", page_icon="🚕", layout="wide")

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
STUDENT_ID_PATTERN = re.compile(r"^\d{8}$")

# 🚨 최고 관리자 계정 정보 (학번: 비밀번호)
ADMIN_CREDS = {
    "60231783": "0422",
    "60231751": "0726"
}

def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)

def to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(KST)

def time_text(value: datetime) -> str:
    return to_kst(value).strftime("%m월 %d일 %H:%M")

def valid_student_id(value: str) -> bool:
    return bool(STUDENT_ID_PATTERN.fullmatch(value.strip()))

@st.cache_resource
def db():
    try:
        key = json.loads(st.secrets["firebase_service_account"])
    except (KeyError, json.JSONDecodeError):
        st.error("Firebase 비밀키가 아직 설정되지 않았습니다. README의 Secrets 설정을 확인해 주세요.")
        st.stop()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(key))
    return firestore.client()

def post_data(snapshot) -> dict:
    value = snapshot.to_dict() or {}
    value["id"] = snapshot.id
    value["participants"] = value.get("participants", {})
    value["participant_count"] = len(value["participants"])
    value["arrived_count"] = sum(bool(p.get("arrived_at")) for p in value["participants"].values())
    value["paid_count"] = sum(bool(p.get("paid_at")) for p in value["participants"].values())
    return value

def live_posts(client, is_admin: bool) -> list[dict]:
    result = []
    for snapshot in client.collection("posts").stream():
        post = post_data(snapshot)
        expiry = post.get("expires_at")
        if expiry and expiry <= now() and not is_admin:
            continue
        result.append(post)
    return sorted(result, key=lambda p: p["departure_at"])

def get_post(client, post_id: str, is_admin: bool) -> dict | None:
    snapshot = client.collection("posts").document(post_id).get()
    if not snapshot.exists:
        return None
    post = post_data(snapshot)
    if post.get("expires_at") and post.get("expires_at") <= now() and not is_admin:
        return None
    return post

def make_post(client, user: str, title: str, body: str, departure: str, destination: str,
              departure_at: datetime, max_people: int, bank: str, account: str) -> str:
    reference = client.collection("posts").document()
    created = now()
    expires_at = created + timedelta(hours=48)
    
    reference.set({
        "author_id": user, "title": title.strip(), "content": body.strip(),
        "departure_place": departure.strip(), "destination": destination.strip(),
        "departure_at": departure_at.astimezone(UTC), "max_people": max_people,
        "bank_name": bank.strip(), "account_number": account.strip(), "created_at": created,
        "expires_at": expires_at, "total_fare": 0, "is_closed": False, "is_payment_requested": False,
        "participants": {user: {"student_id": user, "is_host": True, "joined_at": created,
                                  "on_the_way_at": None, "arrived_at": None, "paid_at": None}},
    })
    return reference.id

def join_post(client, post_id: str, user: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()
    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists: return False, "이미 사라진 택시팟입니다."
        post = post_data(snapshot)
        if post.get("is_closed", False): return False, "이미 모집이 마감된 택시팟입니다."
        participants = dict(post["participants"])
        if user in participants: return True, "이미 참여 중입니다."
        if len(participants) >= post["max_people"]: return False, "모집 인원이 모두 찼습니다."
        participants[user] = {"student_id": user, "is_host": False, "joined_at": now(), "on_the_way_at": None, "arrived_at": None, "paid_at": None}
        transaction.update(reference, {"participants": participants})
        return True, "택시팟에 참여했습니다."
    return run(transaction)

def leave_post(client, post_id: str, target: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()
    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists: return False, "사라진 택시팟입니다."
        post = post_data(snapshot)
        participants = dict(post["participants"])
        if target not in participants: return False, "참여 중이 아닙니다."
        if participants[target].get("is_host"): return False, "방장은 참여 취소가 불가합니다."
        participants.pop(target, None)
        transaction.update(reference, {"participants": participants})
        return True, "참여가 취소되었습니다."
    return run(transaction)

def update_status(client, post_id: str, user: str, field: str) -> None:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()
    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        post = post_data(snapshot)
        participants = dict(post["participants"])
        participant = dict(participants[user])
        participant[field] = now()
        participants[user] = participant
        transaction.update(reference, {"participants": participants})
    run(transaction)

def kick_user(client, post_id: str, author: str, target: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()
    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        post = post_data(snapshot)
        if post["author_id"] != author or target == author:
            return False, "작성자만 내보낼 수 있습니다."
        participants = dict(post["participants"])
        participants.pop(target, None)
        transaction.update(reference, {"participants": participants})
        return True, f"{target}님을 추방했습니다."
    return run(transaction)

def css() -> None:
    st.markdown("""<style>
      /* Streamlit 기본 UI 완벽 숨김 (로고, 메뉴, 하단 워터마크) */
      [data-testid="stHeader"] {display: none !important;}
      [data-testid="stToolbar"] {display: none !important;}
      #MainMenu {visibility: hidden !important;}
      footer {visibility: hidden !important;}

      .block-container{max-width:1080px;padding-top:2.4rem}.sub{color:#788292;font-size:.92rem}
      .card{border:1px solid #dce1e8;border-radius:12px;padding:1rem;margin:.65rem 0}.label{color:#7b8493;font-size:.76rem}.value{font-weight:650;color:#29384f;font-size:.93rem;overflow-wrap:anywhere}.box{background:#f8f8f6;border-radius:10px;padding:.8rem;min-height:82px}.tag{display:inline-block;padding:.16rem .5rem;border-radius:99px;margin-right:.2rem;font-size:.76rem;font-weight:bold;}
      .t-join{background:#e9e4fb;color:#564798} .t-onway{background:#fff4e5;color:#b06000} .t-arrive{background:#e6f4ea;color:#137333} .t-paid{background:#fce8e6;color:#c5221f} .t-wait{background:#f1f2f5;color:#727987}
      .person{border:1px solid #dce1e8;border-radius:9px;padding:.7rem;margin-bottom:.5rem;}
      h1,h2,h3{color:#17253d}div.stButton>button{border-radius:8px}
      div.stButton > button[kind="primary"] {background-color: #042557 !important; border-color: #042557 !important; color: white !important;}
      
      /* 카카오톡 스타일 채팅 UI (배경색 회색으로 변경) */
      .chat-bg { background-color: #ebedf0; padding: 1rem; border-radius: 12px; display: flex; flex-direction: column; gap: 10px; max-height: 400px; overflow-y: auto;}
      .msg-row { display: flex; flex-direction: column; width: 100%; }
      .msg-row.me { align-items: flex-end; }
      .msg-row.other { align-items: flex-start; }
      .msg-author { font-size: 0.75rem; color: #555; margin-bottom: 2px; }
      .msg-row.me .msg-author { margin-right: 4px; }
      .msg-row.other .msg-author { margin-left: 4px; }
      .msg-bubble { padding: 8px 12px; border-radius: 12px; max-width: 75%; font-size: 0.9rem; word-break: break-word; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
      .msg-row.me .msg-bubble { background-color: #FEE500; color: #000; border-top-right-radius: 2px; }
      .msg-row.other .msg-bubble { background-color: #FFFFFF; color: #000; border-top-left-radius: 2px; }
      .msg-time { font-size: 0.65rem; color: #666; margin-top: 2px; }
      </style>""", unsafe_allow_html=True)

def user() -> str:
    return st.session_state.get("student_id", "").strip()

def require_user() -> bool:
    if user(): return True
    st.warning("먼저 로그인을 해주세요.")
    return False

def profile(client) -> None:
    # 학번 수정 제한 기간을 7일에서 365일(1년)으로 연장
    if user() and "student_id_locked_until" not in st.session_state:
        st.session_state.student_id_locked_until = now() + timedelta(days=365)
    locked_until = st.session_state.get("student_id_locked_until")
    locked = bool(locked_until and locked_until > now())
    
    left, right = st.columns([5, 1])
    with left:
        with st.form("profile"):
            st.caption("🚨 첫 로그인 시 비밀번호로 회원가입. 닉네임은 8자리 학번 사용 (1년간 수정 불가).")
            col1, col2 = st.columns([2, 1])
            with col1: student = st.text_input("내 닉네임 (학번)", value=user(), placeholder="예: 60001234", disabled=locked)
            with col2: password = st.text_input("비밀번호 (4자리 이상)", type="password", disabled=locked, placeholder="****")
                
            if st.form_submit_button("로그인 및 저장", use_container_width=True, disabled=locked):
                sid, pwd = student.strip(), password.strip()
                if not valid_student_id(sid): st.error("학번 8자리를 정확히 입력해 주세요.")
                elif not pwd: st.error("비밀번호를 입력해 주세요.")
                else:
                    if sid in ADMIN_CREDS:
                        if pwd == ADMIN_CREDS[sid]:
                            st.session_state.update({"student_id": sid, "is_admin": True, "student_id_locked_until": now() + timedelta(days=365)})
                            st.rerun()
                        else: st.error("관리자 비밀번호 오류")
                    else:
                        user_ref = client.collection("users").document(sid)
                        user_doc = user_ref.get()
                        if user_doc.exists:
                            if user_doc.to_dict().get("password") == pwd:
                                st.session_state.update({"student_id": sid, "is_admin": False, "student_id_locked_until": now() + timedelta(days=365)})
                                st.rerun()
                            else: st.error("비밀번호 오류 (이미 가입된 학번)")
                        else:
                            user_ref.set({"password": pwd})
                            st.session_state.update({"student_id": sid, "is_admin": False, "student_id_locked_until": now() + timedelta(days=365)})
                            st.rerun()
                            
    with right:
        if user():
            st.caption(f"현재: {user()}")
            if st.session_state.get("is_admin", False): st.caption("👑 최고 관리자")

def header(client) -> None:
    a, b = st.columns([5, 1.35])
    with a:
        st.markdown('<h1 style="color:#042557; margin-bottom:0;">🚙 띵지고</h1>', unsafe_allow_html=True)
        # 부제목 문구 정확히 수정 반영
        st.markdown('<p class="sub">셔틀버스를 놓친 명지대 학생들이 빠르게 함께 출발하는 택시팟</p>', unsafe_allow_html=True)
    with b:
        if st.button("＋ 새 택시팟", type="primary", use_container_width=True):
            st.session_state.view = "new"; st.rerun()
    profile(client)

def home(client) -> None:
    is_admin = st.session_state.get("is_admin", False)
    st.header("모든 택시팟")
    posts = live_posts(client, is_admin)
    if not posts: st.info("아직 열린 택시팟이 없어요.")
        
    for post in posts:
        cols = st.columns([2.15, 1.25, 1.2, 1.2, .72, 1.25])
        with cols[0]:
            title_prefix = "<span style='color:red;'>[만료]</span> " if post.get("expires_at") and post.get("expires_at") <= now() else ""
            st.markdown(f'<div class="value" style="font-size:1.1rem">{title_prefix}{html.escape(post["title"])}</div><div class="sub">{html.escape(post["content"][:20])}...</div>', unsafe_allow_html=True)
        for col, label, value in zip(cols[1:5], ["출발 장소", "도착 장소", "출발 시간", "모인 인원"], [post["departure_place"], post["destination"], time_text(post["departure_at"]), f'{post["participant_count"]}/{post["max_people"]}명']):
            with col: st.markdown(f'<div class="label">{label}</div><div class="value">{html.escape(str(value))}</div>', unsafe_allow_html=True)
            
        with cols[5]:
            if is_admin:
                if st.button("🚨 관리자 보기", key=f"admin_{post['id']}", use_container_width=True):
                    st.session_state.view = "detail"; st.session_state.post_id = post["id"]; st.rerun()
            else:
                joined = user() in post["participants"]
                full = post["participant_count"] >= post["max_people"] or post.get("is_closed", False)
                label = "내 상태 보기" if joined else ("모집 마감" if full else "참여하기")
                if st.button(label, key=f"enter_{post['id']}", disabled=full and not joined, use_container_width=True):
                    if require_user():
                        ok, msg = join_post(client, post["id"], user())
                        if ok: st.session_state.view="detail"; st.session_state.post_id=post["id"]; st.rerun()
                        else: st.error(msg)
        st.divider()

def new_post(client) -> None:
    if st.button("← 목록으로"): st.session_state.view="home"; st.rerun()
    st.header("새 택시팟 생성")
    with st.form("new"):
        title = st.text_input("제목")
        body = st.text_area("내용", height=100, placeholder="자유롭게 작성해주세요. 글이 길어지면 칸이 자동으로 늘어납니다.")
        
        a, b = st.columns(2)
        with a: departure = st.text_input("출발 장소")
        with b: destination = st.text_input("도착 장소")
        
        c, d = st.columns(2)
        with c: day = st.date_input("출발 날짜", value=date.today())
        with d: clock = st.time_input("출발 시간", value=time(10,30))
        
        e, f, g = st.columns([1,1.4,.8])
        with e: bank = st.text_input("은행명")
        with f: account = st.text_input("계좌번호")
        with g: maximum = st.selectbox("모일 사람 수", [1,2,3,4], index=3)
        
        submitted = st.form_submit_button("택시팟 생성", type="primary", use_container_width=True)
        
    if submitted:
        if not require_user(): return
        if not all(x.strip() for x in [title,body,departure,destination,bank,account]): st.error("모든 항목을 입력해 주세요."); return
        post_id = make_post(client,user(),title,body,departure,destination,datetime.combine(day,clock,tzinfo=KST),maximum,bank,account)
        st.session_state.view="detail"; st.session_state.post_id=post_id; st.rerun()


def badges(p: dict, is_host: bool, payment_requested: bool) -> str:
    html_str = '<span class="tag t-join">참여</span>'
    html_str += '<span class="tag t-onway">가는 중</span>' if p.get("on_the_way_at") else '<span class="tag t-wait">출발 전</span>'
    html_str += '<span class="tag t-arrive">도착 완료</span>' if p.get("arrived_at") else '<span class="tag t-wait">도착 전</span>'
    
    if is_host:
        html_str += '<span class="tag t-paid">송금요청 완료</span>' if payment_requested else '<span class="tag t-wait">송금요청 전</span>'
    else:
        html_str += '<span class="tag t-paid">송금 완료</span>' if p.get("paid_at") else '<span class="tag t-wait">송금 전</span>'
    return html_str

def detail(client) -> None:
    is_admin = st.session_state.get("is_admin", False)
    post = get_post(client, st.session_state.get("post_id", ""), is_admin)
    if not post: st.warning("사라진 택시팟입니다."); return
    
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("← 목록으로"): st.session_state.view="home"; st.rerun()
    with col2:
        if is_admin:
            if st.button("🚨 관리자 강제 삭제", key="admin_delete"):
                client.collection("posts").document(post["id"]).delete()
                st.session_state.view="home"; st.rerun()
        elif post["author_id"] == user():
            if st.button("🗑️ 방 폭파(삭제)", use_container_width=True):
                client.collection("posts").document(post["id"]).delete()
                st.session_state.view="home"; st.rerun()

    st.header(post["title"]); st.write(post["content"]); st.caption(f'방장: {post["author_id"]}')
    values=[("출발",post["departure_place"]),("도착",post["destination"]),("시간",time_text(post["departure_at"])),("인원",f'{post["participant_count"]}/{post["max_people"]}명')]
    for col,(label,value) in zip(st.columns(4),values):
        with col: st.markdown(f'<div class="box"><div class="label">{label}</div><div class="value">{html.escape(value)}</div></div>',unsafe_allow_html=True)
    
    st.divider()
    st.subheader("💰 정산하기")
    
    is_payment_req = post.get("is_payment_requested", False)
    
    if post["author_id"] == user():
        calc_col1, calc_col2 = st.columns([3, 1])
        with calc_col1:
            fare_input = st.number_input("총 택시 요금 입력 (원)", min_value=0, value=post.get("total_fare", 0), step=100)
        with calc_col2:
            st.write("")
            if st.button("요금 저장", use_container_width=True):
                client.collection("posts").document(post["id"]).update({"total_fare": fare_input})
                st.rerun()
                
    total_fare = post.get("total_fare", 0)
    if total_fare > 0 and post["participant_count"] > 0:
        st.success(f"💸 **1인당 보낼 금액: {total_fare // post['participant_count']:,}원** (총 {total_fare:,}원)")
        
    if is_payment_req or post["author_id"] == user():
        st.info(f"🏦 **송금 계좌:** {post['bank_name']} {post['account_number']}")
        st.markdown('''
            <div style="display: flex; gap: 10px; margin-top: 10px;">
                <a href="supertoss://send" style="flex:1; text-align:center; padding:12px; border-radius:8px; border:1px solid #0050FF; color:#0050FF; text-decoration:none; font-weight:bold; background-color:white;">토스 앱 연결</a>
                <a href="kakaot://" style="flex:1; text-align:center; padding:12px; border-radius:8px; border:1px solid #FFC107; color:#FFC107; text-decoration:none; font-weight:bold; background-color:white;">카카오 T 앱 연결</a>
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.warning("🔒 방장이 도착 후 '송금 요청'을 누르면 계좌번호와 앱 연결 버튼이 열립니다.")

    st.divider()
    st.subheader(f'참여자 {post["participant_count"]}명')
    
    participants = sorted(post["participants"].values(), key=lambda p:(not p.get("is_host"), p["joined_at"]))
    
    for p in participants:
        ident = p["student_id"]
        is_host_user = p.get("is_host", False)
        
        col_name, col_badge, col_btn = st.columns([2.5, 3.5, 4.0])
        
        with col_name:
            st.markdown(f'<div class="person" style="border:none; padding:0;">👤 <b>{html.escape(ident)}</b>{" 👑(방장)" if is_host_user else ""}</div>', unsafe_allow_html=True)
            
        with col_badge:
            st.markdown(f'<div>{badges(p, is_host_user, is_payment_req)}</div>', unsafe_allow_html=True)
            
        with col_btn:
            if ident == user():
                btns = st.columns(4 if not is_host_user else 3)
                if not p.get("on_the_way_at"):
                    if btns[0].button("가는중", key=f'otw{ident}', use_container_width=True): update_status(client,post["id"],ident,"on_the_way_at"); st.rerun()
                elif not p.get("arrived_at"):
                    if btns[1].button("도착", key=f'arr{ident}', use_container_width=True): update_status(client,post["id"],ident,"arrived_at"); st.rerun()
                else:
                    if is_host_user:
                        if not is_payment_req:
                            if btns[2].button("송금요청", key=f'req{ident}', use_container_width=True): 
                                client.collection("posts").document(post["id"]).update({"is_payment_requested": True}); st.rerun()
                    else:
                        if not p.get("paid_at"):
                            if btns[2].button("송금완료", key=f'pay{ident}', use_container_width=True): update_status(client,post["id"],ident,"paid_at"); st.rerun()
                
                if not is_host_user:
                    if btns[-1].button("❌취소", key=f'l{ident}', use_container_width=True):
                        ok, msg = leave_post(client, post["id"], ident)
                        if ok: st.rerun()
                        else: st.error(msg)
            
            elif post["author_id"] == user() and not is_host_user:
                if st.button("❌ 추방", key=f'k{ident}'):
                    ok, msg = kick_user(client, post["id"], user(), ident)
                    if ok: st.rerun()
                    else: st.error(msg)

    st.divider(); st.subheader("실시간 댓글")
    
    comments = list(client.collection("posts").document(post["id"]).collection("comments").order_by("created_at").stream())
    
    st.markdown('<div class="chat-bg">', unsafe_allow_html=True)
    if not comments:
        st.markdown('<div style="text-align:center; color:#888; font-size:0.9rem;">작성된 댓글이 없습니다.</div>', unsafe_allow_html=True)
    for comment in comments:
        c = comment.to_dict()
        is_me = c["author_id"] == user()
        row_class = "me" if is_me else "other"
        
        html_code = f'<div class="msg-row {row_class}">'
        # 작성자 학번 모든 말풍선에 표시 (수정됨)
        html_code += f'<div class="msg-author">{html.escape(c["author_id"])}</div>'
        html_code += f'<div class="msg-bubble">{html.escape(c["body"])}<div class="msg-time">{time_text(c["created_at"])}</div></div></div>'
        st.markdown(html_code, unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    
    with st.form("comment"):
        body = st.text_input("채팅 입력", placeholder="예: 5번 출구 앞에서 만나요!")
        submitted = st.form_submit_button("전송")
    if submitted and require_user() and body.strip():
        client.collection("posts").document(post["id"]).collection("comments").add({"author_id":user(),"body":body.strip(),"created_at":now()})
        st.rerun()

def main() -> None:
    css(); client=db()
    st_autorefresh(interval=3000, key="data_refresh")
    if "view" not in st.session_state: st.session_state.view="home"
    header(client)
    if st.session_state.view=="new": new_post(client)
    elif st.session_state.view=="detail": detail(client)
    else: home(client)

if __name__ == "__main__": main()
