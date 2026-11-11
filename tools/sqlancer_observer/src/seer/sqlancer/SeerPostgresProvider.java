package seer.sqlancer;

import sqlancer.postgres.PostgresProvider;
import sqlancer.postgres.PostgresGlobalState;
import sqlancer.common.oracle.TestOracle;

/** External observation provider; database generation and oracle algorithms remain native. */
public final class SeerPostgresProvider extends PostgresProvider {
    @Override
    public String getDBMSName() {
        return "seer-postgres";
    }

    @Override
    protected TestOracle<PostgresGlobalState> getTestOracle(PostgresGlobalState state) throws Exception {
        return NativeObserver.wrap(state, super.getTestOracle(state), "postgres");
    }
}
