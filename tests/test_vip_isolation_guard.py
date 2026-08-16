"""VIP 数据跨用户隔离护栏 —— fail-closed + 架构接缝 + IDOR 三证（G-5）。

① fail-closed：未绑定用户(_user_id 空)的 store 触碰 VIP 专属表必须显式报错，
   而非静默返回不加 user_id 过滤的全库查询（否则漏 .for_user() 即跨用户泄露）。
② 架构接缝：sim_account/sim_positions 是决策中心与 VIP 共享表，未绑定访问「决策中心口径」
   (account_ref='')不拦；但 VIP 口径(非空 ref)必先经 vip_accounts 解析被拦下——共享表的越权读
   由上游 vip_accounts 卡点等价覆盖，无需直拦共享表而误伤决策中心单用户测试身份。
③ IDOR 免疫：绑定用户后，即便直拦不覆盖的共享表 sim_positions，伪造他人 account_id / 复用他人
   account_ref 仍因 _filtered 追加 AND user_id=? 而读不到他人数据（共享表的主防线是绑定+过滤）。
"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return tmp_path / "wl.db"


def test_unbound_store_vip_exclusive_table_raises(db):
    """未 .for_user() 的 store 触碰 VIP 专属表 → ValueError(安全失败)，绝不静默放行全库查询。"""
    unbound = WatchlistStore(db)          # 未绑定用户：_user_id == ""
    assert not unbound._user_id
    # 端到端：VIP 账户列举直接命中 vip_accounts
    with pytest.raises(ValueError, match="VIP"):
        unbound.list_vip_accounts()
    # 端到端接缝证据：同一 get_sim_account，VIP 口径(非空 ref)先经 ensure_vip_account 读 vip_accounts 被拦
    with pytest.raises(ValueError, match="VIP"):
        unbound.get_sim_account("VIP123")
    # 规范化层与其余 VIP 专属表：raw _filtered 逐一命中
    for sql in (
        "SELECT id, report_md FROM vip_reports WHERE id=?",
        "SELECT * FROM chat_messages WHERE session_id=?",
        "DELETE FROM vip_derivative_terms WHERE id=?",
        "SELECT * FROM positions WHERE account_ref=?",
        "SELECT * FROM transactions WHERE account_ref=?",
        "SELECT * FROM instruments WHERE symbol=?",
    ):
        with pytest.raises(ValueError, match="VIP"):
            unbound._filtered(sql, ("x",))


def test_unbound_store_shared_and_nonvip_tables_pass(db):
    """接缝：决策中心共享表(sim_account/sim_positions)与非 VIP 表在未绑定时维持原语义(放行)——不误伤决策中心。"""
    unbound = WatchlistStore(db)
    # 决策中心自有模拟盘：account_ref='' 不经 vip_accounts，照常建账/读仓，绝不因护栏而炸
    acct = unbound.get_sim_account("")
    assert acct["account_ref"] == "" and acct["id"]
    assert unbound.get_sim_positions() == []
    # 非 VIP 表逐字节不改写、不加 user_id 过滤（对既有单用户路径零副作用）
    q, p = unbound._filtered("SELECT * FROM watchlist WHERE 1=1")
    assert q == "SELECT * FROM watchlist WHERE 1=1" and p == ()
    # \b 词边界：positions 不得误命中 sim_positions（否则会误伤决策中心持仓查询）
    q2, _ = unbound._filtered("SELECT * FROM sim_positions WHERE 1=1")
    assert q2 == "SELECT * FROM sim_positions WHERE 1=1"


def test_bound_store_vip_query_ok_and_cross_user_isolated(db):
    """绑定用户后 VIP 查询正常；sim_positions 上伪造他人 account_id / 复用他人 account_ref 均读不到(IDOR 免疫)。"""
    wl_a = WatchlistStore(db).for_user("userA").for_market("us_stock")
    acct_a = wl_a.get_sim_account("SECRET")                 # A 名下 VIP 账户(绑定 store，vip_accounts 解析不被拦)
    wl_a.create_sim_position(acct_a["id"], "NVDA", 100, 500.0)
    assert len(wl_a.get_sim_positions(acct_a["id"])) == 1   # A 读得到自己的持仓

    wl_b = WatchlistStore(db).for_user("userB").for_market("us_stock")
    # 伪造 A 的 account_id：_filtered 追加 AND user_id='userB' → 空(对象引用越权失败)
    assert wl_b.get_sim_positions(acct_a["id"]) == []
    # 复用相同 account_ref：解析命中的是 B 自己新建的账户行，绝非 A 的行
    acct_b = wl_b.get_sim_account("SECRET")
    assert acct_b["id"] != acct_a["id"]
    assert wl_b.get_sim_positions(acct_b["id"]) == []
