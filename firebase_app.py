"""띵지고 Firebase(공용) 버전. 배포용 실행 파일입니다.

서비스 계정 JSON은 코드에 넣지 말고 Streamlit secrets의
firebase_service_account 항목으로만 제공합니다.
"""

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
    """Streamlit Secrets에서만 서비스 계정 키를 읽어 Firestore를 연결한다."""
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


def live_posts(client) -> list[dict]:
    result = []
    for snapshot in client.collection("posts").stream():
        post = post_data(snapshot)
        expiry = post.get("expires_at")
        if expiry and expiry <= now():
            snapshot.reference.delete()
            continue
        result.append(post)
    return sorted(result, key=lambda p: p["departure_at"])


def get_post(client, post_id: str) -> dict | None:
    snapshot = client.collection("posts").document(post_id).get()
    if not snapshot.exists:
        return None
    post = post_data(snapshot)
    if post.get("expires_at") and post["expires_at"] <= now():
        snapshot.reference.delete()
        return None
    return post


def make_post(client, user: str, title: str, body: str, departure: str, destination: str,
              departure_at: datetime, max_people: int, bank: str, account: str) -> str:
    reference = client.collection("posts").document()
    created = now()
    reference.set({
        "author_id": user, "title": title.strip(), "content": body.strip(),
        "departure_place": departure.strip(), "destination": destination.strip(),
        "departure_at": departure_at.astimezone(UTC), "max_people": max_people,
        "bank_name": bank.strip(), "account_number": account.strip(), "created_at": created,
        "all_paid_at": None, "expires_at": None,
        "participants": {user: {"student_id": user, "is_host": True, "joined_at": created,
                                  "arrived_at": None, "paid_at": None}},
    })
    return reference.id


def refresh_expiry(transaction, reference, post: dict) -> None:
    """작성자를 제외한 모든 참여자가 송금하면 3시간 뒤에 만료 예약한다."""
    guests = [p for p in post["participants"].values() if not p.get("is_host")]
    if guests and all(p.get("paid_at") for p in guests) and not post.get("expires_at"):
        paid_time = now()
        transaction.update(reference, {"all_paid_at": paid_time, "expires_at": paid_time + timedelta(hours=3)})


def join_post(client, post_id: str, user: str) -> tuple[bool, str]:
    reference = client.collection("posts").document(post_id)
    transaction = client.transaction()

    @firestore.transactional
    def run(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists:
            return False, "이미 사라진 택시팟입니다."
        post = post_data(snapshot)
        participants = dict(post["participants"])
        if user in participants:
            return True, "이미 참여 중입니다."
        if len(participants) >= post["max_people"]:
            return False, "모집 인원이 모두 찼습니다."
        participants[user] = {"student_id": user, "is_host": False, "joined_at": now(), "arrived_at": None, "paid_at": None}
        transaction.update(reference, {"participants": participants, "all_paid_at": None, "expires_at": None})
        return True, "택시팟에 참여했습니다."

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
        post["participants"] = participants
        transaction.update(reference, {"participants": participants})
        refresh_expiry(transaction, reference, post)

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
        post["participants"] = participants
        transaction.update(reference, {"participants": participants})
        refresh_expiry(transaction, reference, post)
        return True, f"{target} 참여자를 내보냈습니다."

    return run(transaction)


def css() -> None:
    st.markdown("""<style>
      .block-container{max-width:1080px;padding-top:2.4rem}.sub{color:#788292;font-size:.92rem}
      .card{border:1px solid #dce1e8;border-radius:12px;padding:1rem;margin:.65rem 0}.label{color:#7b8493;font-size:.76rem}.value{font-weight:650;color:#29384f;font-size:.93rem;overflow-wrap:anywhere}.box{background:#f8f8f6;border-radius:10px;padding:.8rem;min-height:82px}.tag{display:inline-block;padding:.16rem .5rem;border-radius:99px;margin-right:.2rem;font-size:.76rem}.join{background:#e9e4fb;color:#564798}.arrive{background:#fff0d7;color:#a8680b}.paid{background:#dff4e9;color:#237b55}.wait{background:#f1f2f5;color:#727987}.person{border:1px solid #dce1e8;border-radius:9px;padding:.7rem}.comment{border-bottom:1px solid #e7e9ed;padding:.7rem 0;white-space:pre-wrap;overflow-wrap:anywhere}h1,h2,h3{color:#17253d}div.stButton>button{border-radius:8px}
      /* 새 택시팟 버튼 네이비색 적용 */
      div.stButton > button[kind="primary"] {background-color: #042557 !important; border-color: #042557 !important; color: white !important;}
      </style>""", unsafe_allow_html=True)


def user() -> str:
    return st.session_state.get("student_id", "").strip()


def require_user() -> bool:
    if user():
        return True
    st.warning("먼저 학번을 저장해 주세요.")
    return False


def profile() -> None:
    if user() and "student_id_locked_until" not in st.session_state:
        st.session_state.student_id_locked_until = now() + timedelta(days=7)
    locked_until = st.session_state.get("student_id_locked_until")
    locked = bool(locked_until and locked_until > now())
    left, right = st.columns([5, 1])
    with left:
        with st.form("profile"):
            student = st.text_input("내 닉네임 (학번)", value=user(), placeholder="예: 60001234", disabled=locked,
                                    help="저장 후 7일 동안 변경할 수 없습니다.")
            if st.form_submit_button("학번 저장", use_container_width=True, disabled=locked):
                st.session_state.student_id = student.strip()
                st.session_state.student_id_locked_until = now() + timedelta(days=7)
                st.rerun()
    with right:
        if user():
            st.caption(f"현재: {user()}")
            st.caption("학번 확인" if valid_student_id(user()) else "학번 미확인")
            if locked:
                st.caption(f"변경 가능: {time_text(locked_until)}")


def header() -> None:
    a, b = st.columns([5, 1.35])
    with a:
        st.markdown('<h1 style="color:#042557; margin-bottom:0;">🚙 띵지고</h1>', unsafe_allow_html=True)
        st.markdown('<p class="sub">셔틀버스를 놓친 명지대 학생들이 빠르게 함께 출발하는 택시팟</p>', unsafe_allow_html=True)
    with b:
        if st.button("＋ 새 택시팟", type="primary", use_container_width=True):
            st.session_state.view = "new"; st.rerun()
    profile()


def home(client) -> None:
    st.header("모든 택시팟")
    st.caption("참여하기를 누르면 바로 참여자 현황과 댓글 화면으로 이동합니다.")
    posts = live_posts(client)
    if not posts:
        st.info("아직 열린 택시팟이 없어요.")
    for post in posts:
        cols = st.columns([2.15, 1.25, 1.2, 1.2, .72, 1.25])
        with cols[0]:
            st.markdown(f'<div class="value" style="font-size:1.1rem">{html.escape(post["title"])}</div><div class="sub">{html.escape(post["content"])}</div>', unsafe_allow_html=True)
        for col, label, value in zip(cols[1:5], ["출발 장소", "도착 장소", "출발 시간", "모인 인원"], [post["departure_place"], post["destination"], time_text(post["departure_at"]), f'{post["participant_count"]}/{post["max_people"]}명']):
            with col: st.markdown(f'<div class="label">{label}</div><div class="value">{html.escape(str(value))}</div>', unsafe_allow_html=True)
        with cols[5]:
            joined = user() in post["participants"]
            full = post["participant_count"] >= post["max_people"]
            label = "내 상태 보기" if joined else ("모집 완료" if full else "참여하기")
            if st.button(label, key=f"enter_{post['id']}", disabled=full and not joined, use_container_width=True):
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


def badges(p: dict) -> str:
    return '<span class="tag join">참여</span>'+('<span class="tag arrive">도착 완료</span>' if p.get("arrived_at") else '<span class="tag wait">도착 전</span>')+('<span class="tag paid">송금 완료</span>' if p.get("paid_at") else '<span class="tag wait">송금 전</span>')


def detail(client) -> None:
    post = get_post(client, st.session_state.get("post_id", ""))
    if not post: st.warning("이 택시팟은 사라졌습니다."); return
    if st.button("← 목록으로"): st.session_state.view="home"; st.rerun()
    st.header(post["title"]); st.write(post["content"]); st.caption(f'글 작성자: {post["author_id"]}')
    values=[("출발 장소",post["departure_place"]),("도착 장소",post["destination"]),("출발 시간",time_text(post["departure_at"])),("모인 인원",f'{post["participant_count"]}/{post["max_people"]}명'),("송금 계좌",f'{post["bank_name"]} {post["account_number"]}')]
    for col,(label,value) in zip(st.columns(5),values):
        with col: st.markdown(f'<div class="box"><div class="label">{label}</div><div class="value">{html.escape(value)}</div></div>',unsafe_allow_html=True)
    
    st.link_button("🔵 토스 앱으로 송금하기", "https://toss.im/", use_container_width=True)
    
    if post.get("expires_at"): st.success(f'모든 참여자의 송금이 완료됐습니다. {time_text(post["expires_at"])}에 글이 사라집니다.')
    st.divider(); st.subheader(f'참여자 {post["participant_count"]}명')
    participants=sorted(post["participants"].values(),key=lambda p:(not p.get("is_host"),p["joined_at"]))
    if user() not in post["participants"] and post["participant_count"] < post["max_people"]:
        if st.button("이 택시팟에 참여하기",type="primary") and require_user():
            ok,msg=join_post(client,post["id"],user());
            if ok: st.rerun()
            st.error(msg)
    for p in participants:
        a,b,c=st.columns([2.4,3.5,2.6]); ident=p["student_id"]
        with a: st.markdown(f'<div class="person">👤 <b>{html.escape(ident)}</b>{" · 작성자" if p.get("is_host") else ""}{" · 학번 미확인" if not valid_student_id(ident) else ""}</div>',unsafe_allow_html=True)
        with b: st.markdown(f'<div class="person">{badges(p)}</div>',unsafe_allow_html=True)
        with c:
            if ident==user():
                x,y=st.columns(2)
                with x:
                    if not p.get("arrived_at") and st.button("도착 완료",key=f'a{ident}',use_container_width=True): update_status(client,post["id"],ident,"arrived_at"); st.rerun()
                with y:
                    if p.get("is_host"): st.caption("작성자는 송금 대상이 아닙니다.")
                    elif not p.get("paid_at") and st.button("송금 완료",key=f'p{ident}',use_container_width=True): update_status(client,post["id"],ident,"paid_at"); st.rerun()
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
    
    # 3000밀리초(3초)마다 자동 새로고침 실행
    st_autorefresh(interval=3000, key="data_refresh")
    
    if "view" not in st.session_state: st.session_state.view="home"
    header()
    if st.session_state.view=="new": new_post(client)
    elif st.session_state.view=="detail": detail(client)
    else: home(client)


if __name__ == "__main__": main()
