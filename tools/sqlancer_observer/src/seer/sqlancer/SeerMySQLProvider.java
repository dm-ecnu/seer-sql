package seer.sqlancer;

import sqlancer.mysql.MySQLProvider;
import sqlancer.mysql.MySQLGlobalState;
import sqlancer.common.oracle.TestOracle;

/** External observation provider; database generation and oracle algorithms remain native. */
public final class SeerMySQLProvider extends MySQLProvider {
    @Override
    public String getDBMSName() {
        return "seer-mysql";
    }

    @Override
    protected TestOracle<MySQLGlobalState> getTestOracle(MySQLGlobalState state) throws Exception {
        return NativeObserver.wrap(state, super.getTestOracle(state), "mysql");
    }
}
