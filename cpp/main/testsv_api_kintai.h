#ifndef API_KINTAI_H_
#define API_KINTAI_H_

//
// 勤怠（QR で打刻）
//
// 店 = テナント（guid で指定）。打刻するのはそのテナントの承認済みメンバー（パスキーでログイン）。
// 扉に貼る固定の QR は /html/clock.html?tenant=<guid>。
//
//   /api/kintai/v0/status          {tenant} 自分の最後の打刻と、次に押すほう（in / out）
//   /api/kintai/v0/punch           {tenant, kind} 打刻する（kind は in = 出勤 / out = 退勤）
//   /api/kintai/v0/month           {tenant, month[, user | all]} 月（"YYYY-MM"、日本時間）の勤務の集計
//                                  （自分の分。user で他の人・all で全員を見られるのはお店の管理者だけ）
//   /api/kintai/v0/request         {tenant, action, punch, kind, at, reason} 打刻の修正をお願いする
//                                  （action は change = 直す / add = 足す / delete = 消す）
//   /api/kintai/v0/requests        {tenant[, mine]} 修正リクエストの一覧（管理者は全員分、ほかは自分の分）
//   /api/kintai/v0/request_cancel  {tenant, id} 自分の承認待ちのリクエストを取り下げる
//   /api/kintai/v0/request_decide  {tenant, id, approve} 承認・却下する（お店の管理者だけ）
//
// 時刻は API ではすべて naive な UTC の "YYYY-MM-DD HH:MM:SS"（日本時間への換算はページ側）。
// 月の集計: その人の打刻を時刻順に並べ、出勤のあとの最初の退勤（24 時間以内）と組にする。
// 0 時をまたいだ勤務は出勤した日（日本時間）の分。組にならない打刻は打刻漏れとして返す。
// 打刻と修正リクエストは 1 年（365 日）で消す（打刻・状況の API の中で 1 時間に 1 回）。
// Python 版は python/testsv_api_kintai.py。検査の順番・失敗時の message・集計の手順を揃えてある。
//

#include <algorithm>
#include <atomic>
#include <map>
#include <set>
#include <vector>

#include "testsv_api_tenant.h"
#include "model_kintai.h"

namespace kintai_api {

    using namespace passkey;
    using namespace tenant_api;

    static const time_t KEEP_SEC = 365LL * 24 * 3600;
    static const time_t SHIFT_MAX_SEC = 24 * 3600;
    static const time_t JST_SEC = 9 * 3600;
    static const time_t FUTURE_SLACK_SEC = 5 * 60;
    static const size_t REASON_MAX_BYTES = 200;
    static const time_t CLEAN_INTERVAL_SEC = 3600;
    static const int REQUESTS_LIMIT = 200;

    static inline bool is_kind(const json& j)
    {
        return j.is_string() && (j == "in" || j == "out");
    }

    static inline json opt_str(bool is_null, const std::string& s)
    {
        return is_null ? json(nullptr) : json(s);
    }

    static inline json opt_time(bool is_null, const std::string& s)
    {
        return is_null ? json(nullptr) : format_time(s);
    }

    // "YYYY-MM-DD HH:MM:SS" をきっちり読む（書き戻して同じ文字列になるものだけ）
    static inline bool parse_time(const json& j, time_t& out)
    {
        if (!j.is_string()) return false;
        std::string s = j;
        if (s.size() != 19) return false;
        for (size_t i = 0; i < 19; i++) {
            char c = s[i];
            bool digit = c >= '0' && c <= '9';
            if (i == 4 || i == 7) { if (c != '-') return false; }
            else if (i == 10) { if (c != ' ') return false; }
            else if (i == 13 || i == 16) { if (c != ':') return false; }
            else if (!digit) return false;
        }
        time_t t = db_to_time(s);
        if (time_to_db(t) != s) return false;
        out = t;
        return true;
    }

    static inline time_t utc_of(int y, int m, int d)
    {
        std::tm tm_buf = {};
        tm_buf.tm_year = y - 1900;
        tm_buf.tm_mon = m - 1;
        tm_buf.tm_mday = d;
#ifdef _WIN32
        return _mkgmtime(&tm_buf);
#else
        return timegm(&tm_buf);
#endif
    }

    // "YYYY-MM"（日本時間の月）→ その月の始まりと次の月の始まり（どちらも UTC）
    static inline bool parse_month(const json& j, time_t& start, time_t& end)
    {
        if (!j.is_string()) return false;
        std::string s = j;
        if (s.size() != 7 || s[4] != '-') return false;
        for (size_t i = 0; i < 7; i++) {
            if (i != 4 && (s[i] < '0' || s[i] > '9')) return false;
        }
        int y = std::atoi(s.substr(0, 4).c_str()), m = std::atoi(s.substr(5, 2).c_str());
        if (y < 2000 || y > 2099 || m < 1 || m > 12) return false;
        start = utc_of(y, m, 1) - JST_SEC;
        end = utc_of(m == 12 ? y + 1 : y, m % 12 + 1, 1) - JST_SEC;
        return true;
    }

    static inline std::string jst_date(time_t t)
    {
        return time_to_db(t + JST_SEC).substr(0, 10);
    }

    static inline bool int_of(const json& body, const char* key, int64_t& out)
    {
        if (!body.is_object() || !body.contains(key) || !body[key].is_number_integer()) return false;
        out = body[key].get<int64_t>();
        return true;
    }

    // 理由は 1〜200 バイト、改行以外の制御文字なし
    static inline bool valid_reason(const json& j)
    {
        if (!j.is_string()) return false;
        std::string s = j;
        if (s.empty() || s.size() > REASON_MAX_BYTES) return false;
        for (unsigned char c : s) {
            if (!((c >= 0x20 && c != 0x7F) || c == 0x0A)) return false;
        }
        return true;
    }

    struct PunchRow {
        int64_t id = -1;
        std::string user, kind, source;
        time_t at = 0;
    };

    struct ReqRow {
        int64_t id = -1;
        std::string user, action, reason, status;
        bool punch_null = true, old_kind_null = true, old_at_null = true, kind_null = true, at_null = true,
            decided_by_null = true, decided_at_null = true, created_at_null = true;
        int64_t punch_id = 0;
        std::string old_kind, old_at, kind, at, decided_by, decided_at, created_at;
    };

    template <typename Row>
    static inline PunchRow to_punch(const Row& a)
    {
        PunchRow p;
        p.id = a.id;
        p.user = a.user.value();
        p.kind = a.kind.value();
        p.source = a.source.value();
        p.at = db_to_time(a.punchedAt.value());
        return p;
    }

    template <typename Row>
    static inline ReqRow to_req(const Row& a)
    {
        ReqRow r;
        r.id = a.id;
        r.user = a.user.value();
        r.action = a.action.value();
        r.reason = a.reason.value();
        r.status = a.status.value();
        r.punch_null = a.punchId.is_null();
        r.punch_id = a.punchId.value();
        r.old_kind_null = a.oldKind.is_null();
        r.old_kind = a.oldKind.value();
        r.old_at_null = a.oldAt.is_null();
        r.old_at = a.oldAt.value();
        r.kind_null = a.kind.is_null();
        r.kind = a.kind.value();
        r.at_null = a.newAt.is_null();
        r.at = a.newAt.value();
        r.decided_by_null = a.decidedBy.is_null();
        r.decided_by = a.decidedBy.value();
        r.decided_at_null = a.decidedAt.is_null();
        r.decided_at = a.decidedAt.value();
        r.created_at_null = a.createdAt.is_null();
        r.created_at = a.createdAt.value();
        return r;
    }

    static inline json punch_to_json(const PunchRow& p)
    {
        json o = json::object();
        o["id"] = p.id;
        o["kind"] = p.kind;
        o["at"] = time_to_db(p.at);
        o["source"] = p.source;
        return o;
    }

    static inline json request_to_json(const ReqRow& r)
    {
        json o = json::object();
        o["id"] = r.id;
        o["user"] = r.user;
        o["action"] = r.action;
        o["punch"] = r.punch_null ? json(nullptr) : json(r.punch_id);
        o["old_kind"] = opt_str(r.old_kind_null, r.old_kind);
        o["old_at"] = opt_time(r.old_at_null, r.old_at);
        o["kind"] = opt_str(r.kind_null, r.kind);
        o["at"] = opt_time(r.at_null, r.at);
        o["reason"] = r.reason;
        o["status"] = r.status;
        o["decided_by"] = opt_str(r.decided_by_null, r.decided_by);
        o["decided_at"] = opt_time(r.decided_at_null, r.decided_at);
        o["created_at"] = opt_time(r.created_at_null, r.created_at);
        return o;
    }

    static inline bool punch_less(const PunchRow& x, const PunchRow& y)
    {
        return x.at != y.at ? x.at < y.at : x.id < y.id;
    }

    // 1 年たった打刻と修正リクエストを消す。1 時間に 1 回だけ
    static inline void cleanup(sql::connection& db)
    {
        static std::atomic<time_t> last{ 0 };
        time_t n = time(NULL);
        time_t prev = last.load();
        if (n - prev < CLEAN_INTERVAL_SEC) return;
        if (!last.compare_exchange_strong(prev, n)) return;
        std::string limit = time_to_db(n - KEEP_SEC);
        const auto ptab = test::kintai::KintaiPunch{};
        const auto rtab = test::kintai::KintaiRequest{};
        db(remove_from(ptab).where(ptab.punchedAt < limit));
        db(remove_from(rtab).where(rtab.createdAt < limit));
    }

    // 店員の入口: ログインしていて、そのテナントの承認済みメンバー
    static inline TenantRow staff_of(sql::connection& db, const std::string& skey, const json& body,
                                     std::string& user, MemberRow& m)
    {
        user = session_user(db, skey);
        TenantRow t = get_tenant(db, body);
        m = tenant_member_of(db, t.id, user);
        return t;
    }

    static inline void require_admin(const MemberRow& m)
    {
        if (m.role_null || m.role != ROLE_ADMIN) throw PasskeyError("tenant admin only");
    }

    static inline std::vector<PunchRow> active_punches(sql::connection& db, int64_t tenant_id, const std::string& user)
    {
        const auto tab = test::kintai::KintaiPunch{};
        std::vector<PunchRow> v;
        for (const auto& a : db(select(all_of(tab)).from(tab)
            .where(tab.tenantId == tenant_id and tab.user == user and tab.deleted == 0))) {
            v.push_back(to_punch(a));
        }
        std::sort(v.begin(), v.end(), punch_less);
        return v;
    }

    // 次に押すほう: 最後の打刻が出勤で、24 時間たっていなければ退勤
    static inline std::string next_kind(const PunchRow* p)
    {
        if (p && p->kind == "in" && time(NULL) - p->at < SHIFT_MAX_SEC) return "out";
        return "in";
    }

    static inline PunchRow get_my_active_punch(sql::connection& db, int64_t tenant_id, const std::string& user,
                                               const json& body)
    {
        int64_t pid;
        if (!int_of(body, "punch", pid)) throw PasskeyError("invalid request");
        const auto tab = test::kintai::KintaiPunch{};
        for (const auto& a : db(select(all_of(tab)).from(tab).where(tab.id == pid and tab.tenantId == tenant_id
            and tab.user == user and tab.deleted == 0))) {
            return to_punch(a);
        }
        throw PasskeyError("unknown punch");
    }

    // 直したあとの時刻: 1 年以内で、先の時刻は 5 分まで
    static inline time_t get_new_time(const json& body)
    {
        time_t t;
        if (!body.contains("at") || !parse_time(body["at"], t)) throw PasskeyError("invalid time");
        time_t n = time(NULL);
        if (t > n + FUTURE_SLACK_SEC || t < n - KEEP_SEC) throw PasskeyError("invalid time");
        return t;
    }

    static inline ReqRow get_request_row(sql::connection& db, int64_t tenant_id, const json& body)
    {
        int64_t rid;
        if (!int_of(body, "id", rid)) throw PasskeyError("invalid request");
        const auto tab = test::kintai::KintaiRequest{};
        for (const auto& a : db(select(all_of(tab)).from(tab).where(tab.id == rid and tab.tenantId == tenant_id))) {
            return to_req(a);
        }
        throw PasskeyError("unknown request");
    }

    static inline ReqRow reload_request(sql::connection& db, int64_t id)
    {
        const auto tab = test::kintai::KintaiRequest{};
        for (const auto& a : db(select(all_of(tab)).from(tab).where(tab.id == id))) {
            return to_req(a);
        }
        throw PasskeyError("unknown request");
    }

    static inline json success_with_request(sql::connection& db, int64_t id)
    {
        json ret = json::object();
        ret["status"] = "success";
        ret["request"] = request_to_json(reload_request(db, id));
        return ret;
    }

    //
    // 月の集計
    //

    struct Shift {
        time_t anchor;
        std::string state;
        const PunchRow* in;
        const PunchRow* out;
    };

    // 1 人分。punches は [start - 24h, end + 24h) の有効な打刻を (時刻, id) の順に
    static inline json summarize(const std::string& user, const std::vector<PunchRow>& punches, time_t start, time_t end)
    {
        time_t n = time(NULL);
        std::vector<Shift> raw;
        auto emit = [&](const std::string& state, const PunchRow* pin, const PunchRow* pout) {
            raw.push_back(Shift{ pin ? pin->at : pout->at, state, pin, pout });
        };
        const PunchRow* pending = nullptr;
        for (const auto& p : punches) {
            if (p.kind == "in") {
                if (pending) emit("missing_out", pending, nullptr);
                pending = &p;
            }
            else {
                if (pending && p.at - pending->at <= SHIFT_MAX_SEC) {
                    emit("ok", pending, &p);
                    pending = nullptr;
                }
                else {
                    if (pending) {
                        emit("missing_out", pending, nullptr);
                        pending = nullptr;
                    }
                    emit("missing_in", nullptr, &p);
                }
            }
        }
        if (pending) emit(n - pending->at < SHIFT_MAX_SEC ? "working" : "missing_out", pending, nullptr);

        json shifts = json::array();
        std::set<std::string> days;
        int64_t total = 0;
        for (const auto& s : raw) {
            if (s.anchor < start || s.anchor >= end) continue;
            json o = json::object();
            o["date"] = jst_date(s.anchor);
            o["state"] = s.state;
            o["in"] = s.in ? punch_to_json(*s.in) : json(nullptr);
            o["out"] = s.out ? punch_to_json(*s.out) : json(nullptr);
            o["minutes"] = nullptr;
            if (s.state == "ok") {
                int64_t minutes = (int64_t)(s.out->at - s.in->at) / 60;
                o["minutes"] = minutes;
                total += minutes;
                days.insert(jst_date(s.anchor));
            }
            shifts.push_back(o);
        }
        json r = json::object();
        r["user"] = user;
        r["days"] = days.size();
        r["minutes"] = total;
        r["shifts"] = shifts;
        return r;
    }

    //
    // API
    //

    static inline json kintai_status(sql::connection& db, const json& body,
                                     const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        cleanup(db);
        std::vector<PunchRow> v = active_punches(db, t.id, user);
        const PunchRow* p = v.empty() ? nullptr : &v.back();
        const auto rtab = test::kintai::KintaiRequest{};
        int64_t pending = 0;
        for (const auto& a : db(select(rtab.id).from(rtab)
            .where(rtab.tenantId == t.id and rtab.user == user and rtab.status == "pending"))) {
            (void)a;
            pending++;
        }
        json ret = json::object();
        ret["status"] = "success";
        ret["tenant"] = t.guid;
        ret["name"] = t.name;
        ret["user"] = user;
        ret["my_role"] = m.role_null ? json(nullptr) : json(m.role);
        ret["last"] = p ? punch_to_json(*p) : json(nullptr);
        ret["next"] = next_kind(p);
        ret["pending"] = pending;
        return ret;
    }

    static inline json kintai_punch(sql::connection& db, const json& body,
                                    const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        if (!body.contains("kind") || !is_kind(body["kind"])) throw PasskeyError("invalid kind");
        std::string kind = body["kind"];
        cleanup(db);
        time_t n = time(NULL);
        const auto tab = test::kintai::KintaiPunch{};
        int64_t id = (int64_t)db(insert_into(tab).set(tab.tenantId = t.id, tab.user = user, tab.kind = kind,
            tab.punchedAt = time_to_db(n), tab.source = "qr", tab.deleted = 0, tab.createdAt = time_to_db(n)));
        PunchRow p;
        p.id = id;
        p.user = user;
        p.kind = kind;
        p.source = "qr";
        p.at = n;
        json ret = json::object();
        ret["status"] = "success";
        ret["punch"] = punch_to_json(p);
        ret["next"] = next_kind(&p);
        return ret;
    }

    static inline json kintai_month(sql::connection& db, const json& body,
                                    const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        time_t start, end;
        if (!body.contains("month") || !parse_month(body["month"], start, end)) throw PasskeyError("invalid month");
        bool all = body.contains("all") && body["all"].is_boolean() && body["all"].get<bool>();
        std::string target;
        if (all) {
            require_admin(m);
        }
        else {
            if (!body.contains("user") || body["user"].is_null() || (body["user"].is_string() && body["user"] == "")) {
                target = user;
            }
            else if (!body["user"].is_string()) {
                throw PasskeyError("invalid request");
            }
            else {
                target = body["user"];
            }
            if (target != user) require_admin(m);
        }
        const auto tab = test::kintai::KintaiPunch{};
        std::string lo = time_to_db(start - SHIFT_MAX_SEC), hi = time_to_db(end + SHIFT_MAX_SEC);
        std::vector<PunchRow> rows;
        for (const auto& a : db(select(all_of(tab)).from(tab).where(tab.tenantId == t.id and tab.deleted == 0
            and tab.punchedAt >= lo and tab.punchedAt < hi))) {
            PunchRow p = to_punch(a);
            if (!all && p.user != target) continue;
            rows.push_back(p);
        }
        std::sort(rows.begin(), rows.end(), punch_less);
        std::vector<std::string> users;
        if (all) {
            std::set<std::string> us;
            const auto mtab = test::tenant::TenantMember{};
            for (const auto& a : db(select(all_of(mtab)).from(mtab)
                .where(mtab.tenantId == t.id and mtab.status == ACCOUNT_APPROVED))) {
                us.insert(a.user.value());
            }
            for (const auto& p : rows) us.insert(p.user);
            users.assign(us.begin(), us.end());   // std::set は文字列のバイト順 = Python の sorted と同じ
        }
        else {
            users.push_back(target);
        }
        json ua = json::array();
        for (const auto& u : users) {
            std::vector<PunchRow> mine;
            for (const auto& p : rows) if (p.user == u) mine.push_back(p);
            ua.push_back(summarize(u, mine, start, end));
        }
        json ret = json::object();
        ret["status"] = "success";
        ret["tenant"] = t.guid;
        ret["month"] = body["month"];
        ret["users"] = ua;
        return ret;
    }

    static inline json kintai_request(sql::connection& db, const json& body,
                                      const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        std::string action = str_of(body, "action");
        if (action != "change" && action != "add" && action != "delete") throw PasskeyError("invalid action");
        if (!body.contains("reason") || !valid_reason(body["reason"])) throw PasskeyError("invalid reason");
        std::string reason = body["reason"];
        const auto rtab = test::kintai::KintaiRequest{};
        PunchRow p;
        bool has_punch = action == "change" || action == "delete";
        if (has_punch) {
            p = get_my_active_punch(db, t.id, user, body);
            if (!db(select(rtab.id).from(rtab).where(rtab.tenantId == t.id and rtab.punchId == p.id
                and rtab.status == "pending")).empty()) {
                throw PasskeyError("already requested");
            }
        }
        std::string kind;
        time_t at = 0;
        bool has_new = action == "change" || action == "add";
        if (has_new) {
            json k = body.contains("kind") ? body["kind"] : json(nullptr);
            if (action == "change" && k.is_null()) k = p.kind;
            if (!is_kind(k)) throw PasskeyError("invalid kind");
            kind = k;
            at = get_new_time(body);
        }
        std::string now = time_to_db(time(NULL));
        int64_t id;
        if (action == "change") {
            id = (int64_t)db(insert_into(rtab).set(rtab.tenantId = t.id, rtab.user = user, rtab.action = action,
                rtab.punchId = p.id, rtab.oldKind = p.kind, rtab.oldAt = time_to_db(p.at), rtab.kind = kind,
                rtab.newAt = time_to_db(at), rtab.reason = reason, rtab.status = "pending", rtab.createdAt = now));
        }
        else if (action == "delete") {
            id = (int64_t)db(insert_into(rtab).set(rtab.tenantId = t.id, rtab.user = user, rtab.action = action,
                rtab.punchId = p.id, rtab.oldKind = p.kind, rtab.oldAt = time_to_db(p.at),
                rtab.reason = reason, rtab.status = "pending", rtab.createdAt = now));
        }
        else {
            id = (int64_t)db(insert_into(rtab).set(rtab.tenantId = t.id, rtab.user = user, rtab.action = action,
                rtab.kind = kind, rtab.newAt = time_to_db(at), rtab.reason = reason, rtab.status = "pending",
                rtab.createdAt = now));
        }
        return success_with_request(db, id);
    }

    static inline json kintai_requests(sql::connection& db, const json& body,
                                       const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        bool mine = (m.role_null || m.role != ROLE_ADMIN) ||
            (body.contains("mine") && body["mine"].is_boolean() && body["mine"].get<bool>());
        const auto tab = test::kintai::KintaiRequest{};
        std::vector<ReqRow> rows;
        for (const auto& a : db(select(all_of(tab)).from(tab).where(tab.tenantId == t.id))) {
            ReqRow r = to_req(a);
            if (mine && r.user != user) continue;
            rows.push_back(r);
        }
        std::sort(rows.begin(), rows.end(), [](const ReqRow& x, const ReqRow& y) { return x.id > y.id; });
        if (rows.size() > (size_t)REQUESTS_LIMIT) rows.resize(REQUESTS_LIMIT);
        json ra = json::array();
        int64_t pending = 0;
        for (const auto& r : rows) {
            ra.push_back(request_to_json(r));
            if (r.status == "pending") pending++;
        }
        json ret = json::object();
        ret["status"] = "success";
        ret["requests"] = ra;
        ret["pending"] = pending;
        return ret;
    }

    static inline json kintai_request_cancel(sql::connection& db, const json& body,
                                             const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        ReqRow r = get_request_row(db, t.id, body);
        if (r.user != user) throw PasskeyError("unknown request");
        if (r.status != "pending") throw PasskeyError("already decided");
        const auto tab = test::kintai::KintaiRequest{};
        db(update(tab).set(tab.status = "canceled", tab.decidedBy = user, tab.decidedAt = time_to_db(time(NULL)))
            .where(tab.id == r.id));
        return success_with_request(db, r.id);
    }

    static inline json kintai_request_decide(sql::connection& db, const json& body,
                                             const std::string& skey, const Request& req)
    {
        std::string user;
        MemberRow m;
        TenantRow t = staff_of(db, skey, body, user, m);
        require_admin(m);
        ReqRow r = get_request_row(db, t.id, body);
        if (r.status != "pending") throw PasskeyError("already decided");
        if (!body.contains("approve") || !body["approve"].is_boolean()) throw PasskeyError("invalid request");
        bool approve = body["approve"].get<bool>();
        std::string now = time_to_db(time(NULL));
        const auto ptab = test::kintai::KintaiPunch{};
        if (approve) {
            if (r.action == "change" || r.action == "delete") {
                if (db(select(ptab.id).from(ptab).where(ptab.id == r.punch_id and ptab.deleted == 0)).empty()) {
                    throw PasskeyError("punch not found");
                }
                db(update(ptab).set(ptab.deleted = 1, ptab.deletedByRequest = r.id).where(ptab.id == r.punch_id));
            }
            if (r.action == "change" || r.action == "add") {
                db(insert_into(ptab).set(ptab.tenantId = t.id, ptab.user = r.user, ptab.kind = r.kind,
                    ptab.punchedAt = format_time(r.at).get<std::string>(), ptab.source = "request",
                    ptab.requestId = r.id, ptab.deleted = 0, ptab.createdAt = now));
            }
        }
        const auto rtab = test::kintai::KintaiRequest{};
        db(update(rtab).set(rtab.status = approve ? "approved" : "rejected", rtab.decidedBy = user,
            rtab.decidedAt = now).where(rtab.id == r.id));
        return success_with_request(db, r.id);
    }

    //
    // テーブル作成と routes
    //

    // Python 版（SQLAlchemy の create_all）と同じ形。どちらが先に作っても同じ表になる。
    static inline void init_kintai_db()
    {
        sql::connection& db = get_sql_connection();
#ifdef USE_MYSQL
        db.execute(R"(
        CREATE TABLE IF NOT EXISTS kintai_punch (
        id int PRIMARY KEY AUTO_INCREMENT,
        tenant_id int NOT NULL,
        user TEXT NOT NULL,
        kind TEXT NOT NULL,
        punched_at DateTime NOT NULL,
        source TEXT NOT NULL,
        request_id int,
        deleted int NOT NULL,
        deleted_by_request int,
        created_at DateTime)
        )");
        db.execute(R"(
        CREATE TABLE IF NOT EXISTS kintai_request (
        id int PRIMARY KEY AUTO_INCREMENT,
        tenant_id int NOT NULL,
        user TEXT NOT NULL,
        action TEXT NOT NULL,
        punch_id int,
        old_kind TEXT,
        old_at DateTime,
        kind TEXT,
        new_at DateTime,
        reason TEXT NOT NULL,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at DateTime,
        created_at DateTime)
        )");
#else
        db.execute(R"(
        CREATE TABLE IF NOT EXISTS kintai_punch (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user TEXT NOT NULL,
        kind TEXT NOT NULL,
        punched_at TEXT NOT NULL,
        source TEXT NOT NULL,
        request_id INTEGER,
        deleted INTEGER NOT NULL,
        deleted_by_request INTEGER,
        created_at TEXT)
        )");
        db.execute(R"(
        CREATE TABLE IF NOT EXISTS kintai_request (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        user TEXT NOT NULL,
        action TEXT NOT NULL,
        punch_id INTEGER,
        old_kind TEXT,
        old_at TEXT,
        kind TEXT,
        new_at TEXT,
        reason TEXT NOT NULL,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        created_at TEXT)
        )");
#endif
    }

    inline static void init_api_kintai(Server& svr)
    {
        init_kintai_db();
        add_route(svr, "/api/kintai/v0/status", kintai_status);
        add_route(svr, "/api/kintai/v0/punch", kintai_punch);
        add_route(svr, "/api/kintai/v0/month", kintai_month);
        add_route(svr, "/api/kintai/v0/request", kintai_request);
        add_route(svr, "/api/kintai/v0/requests", kintai_requests);
        add_route(svr, "/api/kintai/v0/request_cancel", kintai_request_cancel);
        add_route(svr, "/api/kintai/v0/request_decide", kintai_request_decide);
    }

}

#endif
