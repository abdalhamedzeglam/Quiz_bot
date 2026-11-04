"""حالة الجلسات في الذاكرة: جلسة الطالب، حالة معالج الأدمن، الأقفال، وتحديد معدل الضغط."""
import asyncio
import time
from typing import Any, Callable, Dict, Hashable, List, Optional

import config




class SessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[int, Dict[str, Any]] = {}
        self._admin: Dict[int, Dict[str, Any]] = {}
        self._locks: Dict[Hashable, list] = {}        # key -> [Lock, last_used]
        self._last_action: Dict[int, float] = {}

    # ------------------------------------------------------------ الأقفال
    def lock(self, key: Hashable) -> asyncio.Lock:
        """قفل لكل مستخدم: تُنفَّذ إجراءاته بالتسلسل فلا تتداخل ضغطتان له."""
        entry = self._locks.get(key)
        if entry is None:
            entry = [asyncio.Lock(), time.monotonic()]
            self._locks[key] = entry
        entry[1] = time.monotonic()
        return entry[0]

    def throttled(self, uid: int, interval: Optional[float] = None) -> bool:
        interval = config.USER_THROTTLE_SEC if interval is None else interval
        now = time.monotonic()
        last = self._last_action.get(uid)
        if last is not None and now - last < interval:
            return True
        self._last_action[uid] = now
        return False

    # ------------------------------------------------------------ جلسة الطالب
    def start_quiz(self, uid: int, chapter_id: int, q_ids: List[int]) -> Dict[str, Any]:
        s = {
            "chapter_id": chapter_id,
            "q_ids": list(q_ids),
            "idx": 0,                 # رقم السؤال الحالي داخل q_ids
            "state": "ask",           # ask | answered | done
            "chosen": None,           # الخيار الذي اختاره الطالب في السؤال الحالي
            "answers": {},            # question_id -> True/False
            "msg_id": None,           # الرسالة الحية التي تُعدَّل
            "msg_is_photo": False,
            "shown_qid": None,        # السؤال المعروض حالياً في الرسالة
            "retry_ids": [],
            "dirty": False,           # True إن فشل آخر عرض (لإعادة المزامنة)
            "last_activity": time.monotonic(),
        }
        self._sessions[uid] = s
        return s

    def get(self, uid: int) -> Optional[Dict[str, Any]]:
        s = self._sessions.get(uid)
        if s is not None:
            s["last_activity"] = time.monotonic()
        return s

    def clear(self, uid: int) -> None:
        self._sessions.pop(uid, None)

    @staticmethod
    def compute_result(s: Dict[str, Any], exists: Callable[[int], bool]) -> Dict[str, Any]:
        ids = [i for i in s["q_ids"] if exists(i)]
        answers = s["answers"]
        correct = sum(1 for i in ids if answers.get(i) is True)
        wrong = sum(1 for i in ids if answers.get(i) is False)
        return {
            "total": len(ids),
            "correct": correct,
            "wrong": wrong,
            "skipped": len(ids) - correct - wrong,
            "retry_ids": [i for i in ids if answers.get(i) is not True],
        }

    # ------------------------------------------------------------ معالج الأدمن
    def set_admin(self, uid: int, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        self._admin[uid] = {"state": state, "data": data if data is not None else {},
                            "last_activity": time.monotonic()}

    def get_admin(self, uid: int) -> Optional[Dict[str, Any]]:
        st = self._admin.get(uid)
        if st is not None:
            st["last_activity"] = time.monotonic()
        return st

    def clear_admin(self, uid: int) -> None:
        self._admin.pop(uid, None)

    # ------------------------------------------------------------ تنظيف
    def active_count(self) -> int:
        return len(self._sessions)

    def cleanup(self) -> None:
        now = time.monotonic()
        ttl = config.SESSION_TTL_SEC
        for uid in [u for u, s in self._sessions.items() if now - s["last_activity"] > ttl]:
            self._sessions.pop(uid, None)
        for uid in [u for u, s in self._admin.items() if now - s["last_activity"] > ttl]:
            self._admin.pop(uid, None)
        for uid in [u for u, t in self._last_action.items() if now - t > 60]:
            self._last_action.pop(uid, None)
        for key in [k for k, (lk, used) in self._locks.items() if now - used > ttl and not lk.locked()]:
            self._locks.pop(key, None)


sessions = SessionManager()
