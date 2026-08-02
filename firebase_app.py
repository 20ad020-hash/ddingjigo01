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
        is_expired = expiry and expiry <= now()
        
        # 일반 유저에게는 만료된 글을 숨김 (DB에서 영구 삭제하지 않음)
        if is_expired and not is_admin:
            continue
            
        result.append(post)
    return sorted(result, key=lambda p: p["departure_at"])


def get_post(client, post_id: str, is_admin: bool) -> dict | None:
    snapshot = client.collection("posts").document(post_id).get()
    if not snapshot.exists:
        return None
    post = post_data(snapshot)
    
    # 일반 유저가 만료된 글에 접근하려고 하면 차단
    if post.get("expires_at") and post.get("expires_at") <= now() and not is_admin:
        return None
        
    return post


def make_post(client, user: str, title: str, body: str, departure: str, destination: str,
              departure_at: datetime, max_people: int, bank: str, account: str) -> str:
    reference = client.collection("posts").document()
    created = now()
    # 글 작성 시점으로부터 정확히 48시간 뒤로 만료 시간 설정
    expires_at = created + timedelta(hours=48)
    
    reference.set({
        "author_id": user, "title": title.strip(), "content": body.strip(),
        "departure_place": departure.strip(), "destination": destination.strip(),
        "departure_at": departure_at.astimezone(UTC), "max_people": max_people,
        "bank_name": bank.strip(), "account_number": account.strip(), "created_at": created,
        "expires_at": expires_at, "total_fare": 0, "is_closed": False,
        "participants": {user: {"student_id": user, "is_host": True, "joined_at": created,
                                  "arrived_at": None, "paid_at": None}},
    })
    return reference.id


def update_post(client, post_id: str, title: str, body: str, departure: str, destination: str,
                departure_at: datetime, max_people: int, bank: str, account: str) -> None:
    reference = client.collection("posts").document(post_id)
    reference.update({
        "title": title.strip(), "content": body.strip(),
        "departure_place": departure.strip(), "destination": destination.strip(),
        "departure_at": departure_at.astimezone(UTC), "max_people": max_people,
        "bank_name": bank.strip(), "account_number": account.strip()
    })


def join_post(client, post_id: str, user: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()

    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists:
            return False, "이미 사라진 택시팟입니다."
        post = post_data(snapshot)
        if post.get("is_closed", False):
            return False, "이미 모집이 마감된 택시팟입니다."
        participants = dict(post["participants"])
        if user in participants:
            return True, "이미 참여 중입니다."
        if len(participants) >= post["max_people"]:
            return False, "모집 인원이 모두 찼습니다."
        participants[user] = {"student_id": user, "is_host": False, "joined_at": now(), "arrived_at": None, "paid_at": None}
        transaction.update(reference, {"participants": participants})
        return True, "택시팟에 참여했습니다."

    return run(transaction)


def leave_post(client, post_id: str, target: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()

    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists:
            return False, "이미 사라진 택시팟입니다."
        post = post_data(snapshot)
        participants = dict(post["participants"])
        if target not in participants:
            return False, "참여 중이 아닙니다."
        if participants[target].get("is_host"):
            return False, "방장은 참여 취소를 할 수 없습니다. 글 삭제를 이용해주세요."
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


def kick_unverified(client, post_id: str, author: str, target: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()

    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        post = post_data(snapshot)
        if post["author_id"] != author or target == author:
            return False, "작성자만 내보낼 수 있습니다."
        if valid_student_id(target):
            return False, "학번 형식이 확인된 참여자는 내보낼 수 없습니다."
        participants = dict(post["participants"])
        participants.pop(target, None)
        transaction.update(reference, {"participants": participants})
        return True, f"{target} 참여자를 내보냈습니다."

    return run(transaction)


def css() -> None:
    st.markdown("""<style>
      .block-container{max-width:1080px;padding-top:2.4rem}.sub{color:#788292;font-size:.92rem}
      .card{border:1px solid #dce1e8;border-radius:12px;padding:1rem;margin:.65rem 0}.label{color:#7b8493;font-size:.76rem}.value{font-weight:650;color:#29384f;font-size:.93rem;overflow-wrap:anywhere}.box{background:#f8f8f6;border-radius:10px;padding:.8rem;min-height:82px}.tag{display:inline-block;padding:.16rem .5rem;border-radius:99px;margin-right:.2rem;font-size:.76rem}.join{background:#e9e4fb;color:#564798}.arrive{background:#fff0d7;color:#a8680b}.paid{background:#dff4e9;color:#237b55}.wait{background:#f1f2f5;color:#727987}.person{border:1px solid #dce1e8;border-radius:9px;padding:.7rem}.comment{border-bottom:1px solid #e7e9ed;padding:.7rem 0;white-space:pre-wrap;overflow-wrap:anywhere}h1,h2,h3{color:#17253d}div.stButton>button{border-radius:8px}
      div.stButton > button[kind="primary"] {background-color: #042557 !important; border-color: #042557 !important; color: white !important;}
      </style>""", unsafe_allow_html=True)


def user() -> str:
    return st.session_state.get("student_id", "").strip()


def require_user() -> bool:
    if user():
        return True
    st.warning("먼저 로그인을 해주세요.")
    return False


def profile(client) -> None:
    if user() and "student_id_locked_until" not in st.session_state:
        st.session_state.student_id_locked_until = now() + timedelta(days=7)
    locked_until = st.session_state.get("student_id_locked_until")
    locked = bool(locked_until and locked_until > now())
    
    left, right = st.columns([5, 1])
    with left:
        with st.form("profile"):
            st.caption("🚨 첫 로그인 시 입력한 비밀번호로 회원가입이 됩니다. 닉네임은 무조건 8자리 학번을 사용하세요.")
            
            # 사용자 로그인 폼
            col1, col2 = st.columns([2, 1])
            with col1:
                student = st.text_input("내 닉네임 (학번)", value=user(), placeholder="예: 60001234", disabled=locked)
            with col2:
                password = st.text_input("비밀번호 (4자리 이상)", type="password", disabled=locked, placeholder="****")
                
            if st.form_submit_button("로그인 및 저장", use_container_width=True, disabled=locked):
                sid = student.strip()
                pwd = password.strip()
                
                if not valid_student_id(sid):
                    st.error("명지대 학번 8자리를 정확히 입력해 주세요.")
                elif not pwd:
                    st.error("비밀번호를 입력해 주세요.")
                else:
                    # 최고 관리자 로그인 처리
                    if sid in ADMIN_CREDS:
                        if pwd == ADMIN_CREDS[sid]:
                            st.session_state.student_id = sid
                            st.session_state.is_admin = True
                            st.session_state.student_id_locked_until = now() + timedelta(days=7)
                            st.rerun()
                        else:
                            st.error("관리자 비밀번호가 틀렸습니다.")
                    else:
                        # 일반 유저 로그인 및 가입 처리
                        user_ref = client.collection("users").document(sid)
                        user_doc = user_ref.get()
                        
                        if user_doc.exists:
                            saved_pwd = user_doc.to_dict().get("password")
                            if saved_pwd == pwd:
                                st.session_state.student_id = sid
                                st.session_state.is_admin = False
                                st.session_state.student_id_locked_until = now() + timedelta(days=7)
                                st.rerun()
                            else:
                                st.error("비밀번호가 틀렸습니다. (이미 가입된 학번입니다)")
                        else:
                            # 새 유저 등록
                            user_ref.set({"password": pwd})
                            st.session_state.student_id = sid
                            st.session_state.is_admin = False
                            st.session_state.student_id_locked_until = now() + timedelta(days=7)
                            st.rerun()
                            
    with right:
        if user():
            st.caption(f"현재: {user()}")
            st.caption("학번 확인" if valid_student_id(user()) else "학번 미확인")
            if st.session_state.get("is_admin", False):
                st.caption("👑 최고 관리자 계정")
            elif locked:
                st.caption(f"변경 가능: {time_text(locked_until)}")


def header(client) -> None:
    a, b = st.columns([5, 1.35])
    with a:
        st.markdown('<h1 style="color:#042557; margin-bottom:0;">🚙 띵지고</h1>', unsafe_allow_html=True)
        st.markdown('<p class="sub">셔틀버스를 놓친 명지대 학생들이 빠르게 함께 출발하는 택시팟</p>', unsafe_allow_html=True)
    with b:
        if st.button("＋ 새 택시팟", type="primary", use_container_width=True):
            st.session_state.view = "new"; st.rerun()
    profile(client)


def home(client) -> None:
    is_admin = st.session_state.get("is_admin", False)
    st.header("모든 택시팟")
    
    with st.expander("📖 띵지고 사용설명서 (처음 오셨다면 꼭 읽어주세요!)"):
        st.markdown("""
        **1. 시작하기 전 필수 세팅**
        * 화면 상단에 본인의 **명지대 학번(8자리 숫자)**과 **비밀번호**를 입력해 주세요. (최초 입력 시 자동 가입됩니다)
        * 학번이 아닐 경우, 언제든 강퇴당할 수 있습니다.
        
        **2. 택시팟 모이기 & 소통하기**
        * 리스트에서 원하는 택시팟에 '참여하기'를 누릅니다. (참여자는 언제든 '참여 취소'가 가능합니다)
        * 원하는 인원이 다 모이지 않았더라도, **방장(글 작성자)이 판단하여 '모집 조기 마감' 버튼을 눌러 출발을 확정할 수 있습니다.**
        * 방장은 등록한 글을 수정하거나 삭제할 수도 있습니다.
        * 모임 장소에 도착하면 **'도착 완료'** 버튼을 눌러주세요.
        
        **3. 하차 및 자동 정산 (가장 중요!)**
        * 목적지에 내리기 직전, 글 작성자(방장)가 택시 미터기 총액(또는 중간 정산 금액)을 입력할 수 있습니다.
        * 금액을 입력하면 인원수에 맞춰 **1인당 송금액**이 자동으로 계산됩니다. (단순 부가 기능이므로, 금액을 공란으로 두거나 0원으로 남겨두어도 시스템 이용에는 아무 문제가 없습니다.)
        * 금액을 확인하고, **'토스'**나 **'카카오 T'** 앱 열기 버튼을 눌러 작성자에게 송금합니다.
        * 송금을 마친 분은 반드시 **'송금 완료'** 버튼을 눌러 상태를 변경해 주세요. (방장은 돈을 받는 사람이므로 누르지 않습니다.)
        """)
        
        try:
            st.image("ex1.jpg", caption="[참고] 사용 예시 1")
            st.image("ex2.jpg", caption="[참고] 사용 예시 2")
        except:
            pass

    if is_admin:
        st.info("👑 최고 관리자 모드: 만료된 모든 게시글까지 확인 가능하며, 글을 강제로 열람하고 삭제할 수 있습니다.")
    else:
        st.caption("참여하기를 누르면 바로 참여자 현황과 댓글 화면으로 이동합니다. (모든 글은 작성 48시간 뒤 자동 숨김 처리됩니다)")
        
    posts = live_posts(client, is_admin)
    if not posts:
        st.info("아직 열린 택시팟이 없어요.")
        
    for post in posts:
        cols = st.columns([2.15, 1.25, 1.2, 1.2, .72, 1.25])
        with cols[0]:
            is_expired = post.get("expires_at") and post.get("expires_at") <= now()
            title_prefix = "<span style='color:red;'>[만료]</span> " if is_expired else ""
            st.markdown(f'<div class="value" style="font-size:1.1rem">{title_prefix}{html.escape(post["title"])}</div><div class="sub">{html.escape(post["content"])}</div>', unsafe_allow_html=True)
            
        for col, label, value in zip(cols[1:5], ["출발 장소", "도착 장소", "출발 시간", "모인 인원"], [post["departure_place"], post["destination"], time_text(post["departure_at"]), f'{post["participant_count"]}/{post["max_people"]}명']):
            with col: st.markdown(f'<div class="label">{label}</div><div class="value">{html.escape(str(value))}</div>', unsafe_allow_html=True)
            
        with cols[5]:
            if is_admin:
                if st.button("🚨 관리자 보기", key=f"admin_{post['id']}", use_container_width=True):
                    st.session_state.view = "detail"
                    st.session_state.post_id = post["id"]
                    st.rerun()
            else:
                joined = user() in post["participants"]
                is_closed = post.get("is_closed", False)
                full = post["participant_count"] >= post["max_people"]
                
                label = "내 상태 보기" if joined else ("모집 마감" if (full or is_closed) else "참여하기")
                
                if st.button(label, key=f"enter_{post['id']}", disabled=(full or is_closed) and not joined, use_container_width=True):
                    if require_user():
                        ok, message = join_post(client, post["id"], user())
                        if ok: st.session_state.view="detail"; st.session_state.post_id=post["id"]; st.rerun()
                        st.error(message)
                        
        st.caption(f'도착 {post["arrived_count"]}명 · 송금 완료 {post["paid_count"]}명')
        st.divider()


def new_post(client) -> None:
    if st.button("← 목록으로"): st.session_state.view="home"; st.rerun()
    st.header("새 택시팟 만들기")
    with st.form("new"):
        title=st.text_input("제목"); body=st.text_area("내용", max_chars=300)
        a,b=st.columns(2)
        with a: departure=st.text_input("출발 장소"); day=st.date_input("출발 날짜", value=date.today())
        with b: destination=st.text_input("도착 장소"); clock=st.time_input("출발 시간", value=time(10,30))
        a,b,c=st.columns([1,1.4,.8])
        with a: bank=st.text_input("은행명")
        with b: account=st.text_input("계좌번호")
        with c: maximum=st.selectbox("모일 사람 수", [1,2,3,4], index=3)
        submitted=st.form_submit_button("택시팟 등록", type="primary", use_container_width=True)
    if submitted:
        if not require_user(): return
        if not all(x.strip() for x in [title,body,departure,destination,bank,account]): st.error("모든 항목을 입력해 주세요."); return
        post_id=make_post(client,user(),title,body,departure,destination,datetime.combine(day,clock,tzinfo=KST),maximum,bank,account)
        st.session_state.view="detail"; st.session_state.post_id=post_id; st.rerun()


def edit_post(client) -> None:
    post_id = st.session_state.get("post_id")
    is_admin = st.session_state.get("is_admin", False)
    post = get_post(client, post_id, is_admin)
    
    if not post or post["author_id"] != user():
        st.warning("수정 권한이 없거나 글이 존재하지 않습니다.")
        if st.button("← 돌아가기"): st.session_state.view = "home"; st.rerun()
        return

    if st.button("← 취소하고 돌아가기"): st.session_state.view = "detail"; st.rerun()
    st.header("택시팟 수정하기")
    
    kst_dt = to_kst(post["departure_at"])
    
    with st.form("edit"):
        title = st.text_input("제목", value=post["title"])
        body = st.text_area("내용", value=post["content"], max_chars=300)
        a, b = st.columns(2)
        with a: 
            departure = st.text_input("출발 장소", value=post["departure_place"])
            day = st.date_input("출발 날짜", value=kst_dt.date())
        with b: 
            destination = st.text_input("도착 장소", value=post["destination"])
            clock = st.time_input("출발 시간", value=kst_dt.time())
        a, b, c = st.columns([1, 1.4, .8])
        with a: bank = st.text_input("은행명", value=post["bank_name"])
        with b: account = st.text_input("계좌번호", value=post["account_number"])
        
        current_max = post["max_people"]
        idx = [1,2,3,4].index(current_max) if current_max in [1,2,3,4] else 3
        with c: maximum = st.selectbox("모일 사람 수", [1,2,3,4], index=idx)
        
        submitted = st.form_submit_button("수정 완료", type="primary", use_container_width=True)
        
    if submitted:
        if not all(x.strip() for x in [title, body, departure, destination, bank, account]):
            st.error("모든 항목을 입력해 주세요.")
            return
        update_post(client, post_id, title, body, departure, destination, datetime.combine(day, clock, tzinfo=KST), maximum, bank, account)
        st.session_state.view = "detail"
        st.rerun()


# 🚨 누락되었던 뱃지 함수 (이 부분 때문에 에러가 났습니다!)
def badges(p: dict) -> str:
    return '<span class="tag join">참여</span>'+('<span class="tag arrive">도착 완료</span>' if p.get("arrived_at") else '<span class="tag wait">도착 전</span>')+('<span class="tag paid">송금 완료</span>' if p.get("paid_at") else '<span class="tag wait">송금 전</span>')


def detail(client) -> None:
    is_admin = st.session_state.get("is_admin", False)
    post = get_post(client, st.session_state.get("post_id", ""), is_admin)
    if not post: st.warning("이 택시팟은 사라졌습니다."); return
    
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("← 목록으로"): st.session_state.view="home"; st.rerun()
    with col2:
        if is_admin:
            if st.button("🚨 관리자 권한으로 이 택시팟 영구 삭제", key="admin_delete"):
                client.collection("posts").document(post["id"]).delete()
                st.session_state.view="home"
                st.rerun()
        # 방장의 경우 수정/삭제 버튼 제공
        elif post["author_id"] == user():
            btn_e, btn_d = st.columns(2)
            with btn_e:
                if st.button("✏️ 수정", use_container_width=True):
                    st.session_state.view = "edit"
                    st.rerun()
            with btn_d:
                if st.button("🗑️ 삭제", use_container_width=True):
                    client.collection("posts").document(post["id"]).delete()
                    st.session_state.view = "home"
                    st.rerun()

    st.header(post["title"]); st.write(post["content"]); st.caption(f'글 작성자: {post["author_id"]}')
    values=[("출발 장소",post["departure_place"]),("도착 장소",post["destination"]),("출발 시간",time_text(post["departure_at"])),("모인 인원",f'{post["participant_count"]}/{post["max_people"]}명'),("송금 계좌",f'{post["bank_name"]} {post["account_number"]}')]
    for col,(label,value) in zip(st.columns(5),values):
        with col: st.markdown(f'<div class="box"><div class="label">{label}</div><div class="value">{html.escape(value)}</div></div>',unsafe_allow_html=True)
    
    st.divider()
    st.subheader("💰 정산하기")
    
    # 방장만 총 요금을 입력할 수 있도록 구성
    if post["author_id"] == user():
        calc_col1, calc_col2 = st.columns([3, 1])
        with calc_col1:
            fare_input = st.number_input("총 택시 요금 또는 정산 금액을 입력하세요 (원)", min_value=0, value=post.get("total_fare", 0), step=100, label_visibility="collapsed")
        with calc_col2:
            if st.button("요금 공유하기", use_container_width=True):
                client.collection("posts").document(post["id"]).update({"total_fare": fare_input})
                st.rerun()
                
    total_fare = post.get("total_fare", 0)
    if total_fare > 0 and post["participant_count"] > 0:
        per_person = total_fare // post["participant_count"]
        st.success(f"🚕 **총 요금:** {total_fare:,}원 ➔ 💸 **1인당 보내야 할 금액: {per_person:,}원** (총 {post['participant_count']}명 기준)")
    else:
        st.info("💡 방장이 택시 요금을 입력하면 1인당 송금액이 자동 계산됩니다. (입력하지 않아도 시스템 이용에 문제없는 부가 기능입니다)")

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        st.link_button("🔵 토스 앱으로 송금하기", "https://toss.im/", use_container_width=True)
    with btn_col2:
        st.link_button("🟡 카카오 T 앱 열기", "kakaot://", use_container_width=True)
    
    if post.get("expires_at"):
        st.info(f'이 게시글은 작성 후 48시간 뒤인 {time_text(post["expires_at"])}에 자동 숨김 처리됩니다.')
        
    st.divider(); st.subheader(f'참여자 {post["participant_count"]}명')
    
    is_closed = post.get("is_closed", False)
    if is_closed:
        st.warning("🚫 방장에 의해 모집이 마감된 택시팟입니다.")
    elif post["author_id"] == user() and post["participant_count"] < post["max_people"]:
        if st.button("🚫 원하는 인원이 안 모였어도 모집 마감하기", use_container_width=True):
            client.collection("posts").document(post["id"]).update({"is_closed": True})
            st.rerun()
            
    st.caption("⚠️ 주의: 참여자의 닉네임이 올바른 학번(8자리 숫자)이 아닐 경우, 작성자가 '학번 미확인 추방'을 할 수 있습니다.")
    
    participants=sorted(post["participants"].values(),key=lambda p:(not p.get("is_host"),p["joined_at"]))
    
    # 관리자가 아니고, 참여 안 했고, 인원 남았고, 안 닫혔을 때만 참여 버튼 표시
    if not is_admin and user() not in post["participants"] and post["participant_count"] < post["max_people"] and not is_closed:
        if st.button("이 택시팟에 참여하기",type="primary") and require_user():
            ok,msg=join_post(client,post["id"],user());
            if ok: st.rerun()
            st.error(msg)
            
    for p in participants:
        a,b,c=st.columns([2.2, 2.8, 4.0]); ident=p["student_id"]
        with a: st.markdown(f'<div class="person">👤 <b>{html.escape(ident)}</b>{" · 작성자" if p.get("is_host") else ""}{" · 학번 미확인" if not valid_student_id(ident) else ""}</div>',unsafe_allow_html=True)
        with b: st.markdown(f'<div class="person">{badges(p)}</div>',unsafe_allow_html=True)
        with c:
            if ident==user():
                # 방장인 경우
                if p.get("is_host"):
                    x, y = st.columns(2)
                    with x:
                        if not p.get("arrived_at") and st.button("도착 완료",key=f'a{ident}',use_container_width=True): update_status(client,post["id"],ident,"arrived_at"); st.rerun()
                    with y:
                        st.caption("방장(수정/삭제 권한 보유)")
                # 일반 참여자인 경우 (취소 버튼 포함)
                else:
                    x, y, z = st.columns([1, 1, 1])
                    with x:
                        if not p.get("arrived_at") and st.button("도착",key=f'a{ident}',use_container_width=True): update_status(client,post["id"],ident,"arrived_at"); st.rerun()
                    with y:
                        if not p.get("paid_at") and st.button("송금",key=f'p{ident}',use_container_width=True): update_status(client,post["id"],ident,"paid_at"); st.rerun()
                    with z:
                        if st.button("❌ 취소",key=f'l{ident}',use_container_width=True): 
                            ok,msg = leave_post(client, post["id"], ident)
                            if ok: st.rerun()
                            else: st.error(msg)
            elif post["author_id"]==user() and not p.get("is_host") and not valid_student_id(ident):
                if st.button("학번 미확인 추방",key=f'k{ident}'):
                    ok,msg=kick_unverified(client,post["id"],user(),ident); st.success(msg) if ok else st.error(msg); st.rerun() if ok else None
            else: st.caption("본인만 상태를 변경할 수 있어요.")
            
    st.divider(); st.subheader("댓글")
    comments=list(client.collection("posts").document(post["id"]).collection("comments").order_by("created_at").stream())
    for comment in comments:
        c=comment.to_dict(); st.markdown(f'<div class="comment"><b>{html.escape(c["author_id"])}</b> <span class="sub">{time_text(c["created_at"])}</span><br>{html.escape(c["body"])}</div>',unsafe_allow_html=True)
    with st.form("comment"):
        body=st.text_area("댓글 작성",placeholder="예: 5번 출구 앞에서 만나요!",max_chars=300); submitted=st.form_submit_button("댓글 등록")
    if submitted:
        if require_user() and body.strip(): client.collection("posts").document(post["id"]).collection("comments").add({"author_id":user(),"body":body.strip(),"created_at":now()}); st.rerun()


def main() -> None:
    css(); client=db()
    
    st_autorefresh(interval=3000, key="data_refresh")
    
    if "view" not in st.session_state: st.session_state.view="home"
    header(client)
    
    if st.session_state.view=="new": new_post(client)
    elif st.session_state.view=="edit": edit_post(client)
    elif st.session_state.view=="detail": detail(client)
    else: home(client)


if __name__ == "__main__": main()
