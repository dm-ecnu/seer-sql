package seer.sqlancer;

import sqlancer.sqlite3.SQLite3Provider;
import sqlancer.sqlite3.SQLite3GlobalState;
import sqlancer.common.oracle.TestOracle;

/** External observation provider; database generation and oracle algorithms remain native. */
public final class SeerSQLite3Provider extends SQLite3Provider {
    @Override
    public String getDBMSName() {
        return "seer-sqlite3";
    }

    @Override
    protected TestOracle<SQLite3GlobalState> getTestOracle(SQLite3GlobalState state) throws Exception {
        return NativeObserver.wrap(state, super.getTestOracle(state), "sqlite3");
    }
}
