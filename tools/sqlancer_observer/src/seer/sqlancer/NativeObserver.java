package seer.sqlancer;

import java.io.OutputStream;
import java.io.File;
import java.io.RandomAccessFile;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.Properties;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import sqlancer.GlobalState;
import sqlancer.Main;
import sqlancer.IgnoreMeException;
import sqlancer.Reproducer;
import sqlancer.common.gen.NoRECGenerator;
import sqlancer.common.oracle.NoRECOracle;
import sqlancer.common.oracle.CompositeTestOracle;
import sqlancer.common.oracle.TLPWhereOracle;
import sqlancer.common.oracle.TestOracle;

/** Raw native-oracle observations only; a successful check is not an EQ label.
 * Pinned to SQLancer 1d797ee53631df2fe574468e2be0425985f39867.
 * No SQL rewriting, additional generator calls, or native algorithm changes.
 */
public final class NativeObserver<G extends GlobalState<?, ?, ?>> implements TestOracle<G> {
    private static final ConcurrentHashMap<Path, AtomicLong> GENERATIONS = new ConcurrentHashMap<>();
    private static final Set<String> OTHER_TLP = Set.of(
            "sqlancer.sqlite3.oracle.tlp.SQLite3TLPDistinctOracle",
            "sqlancer.sqlite3.oracle.tlp.SQLite3TLPGroupByOracle",
            "sqlancer.sqlite3.oracle.tlp.SQLite3TLPHavingOracle",
            "sqlancer.sqlite3.oracle.tlp.SQLite3TLPAggregateOracle",
            "sqlancer.postgres.oracle.tlp.PostgresTLPHavingOracle",
            "sqlancer.postgres.oracle.tlp.PostgresTLPAggregateOracle");
    private final G state;
    private final Path nativeLog;
    private final TestOracle<G> delegate;
    private final Path directory;
    private final String setupDigest;
    private final String oracleName;
    private final Properties generated = new Properties();
    private long sequence;

    private NativeObserver(G state, TestOracle<G> delegate, String engine) throws Exception {
        this.delegate = delegate;
        this.state = state;
        oracleName = delegate.getClass().getName();
        if (delegate.getClass() != NoRECOracle.class && delegate.getClass() != TLPWhereOracle.class
                && !OTHER_TLP.contains(oracleName)) {
            throw new IllegalArgumentException("Unobserved native oracle type: " + oracleName);
        }
        if (!state.getOptions().logEachSelect()) {
            throw new IllegalArgumentException("Native per-select logging required for complete raw observations");
        }
        Field logField = Main.StateLogger.class.getDeclaredField("curFile");
        logField.setAccessible(true);
        nativeLog = ((File) logField.get(state.getLogger())).toPath().toAbsolutePath();
        String destination = System.getProperty("seer.sqlancer.observations");
        if (destination == null || !Path.of(destination).isAbsolute()) {
            throw new IllegalArgumentException("Absolute -Dseer.sqlancer.observations required");
        }
        String db = state.getState().getDatabaseName();
        String encoded = Base64.getUrlEncoder().withoutPadding().encodeToString(db.getBytes(StandardCharsets.UTF_8));
        Path databaseRoot = Path.of(destination).resolve(engine + "-" + encoded + "-" + state.getState().getSeedValue());
        long generation = GENERATIONS.computeIfAbsent(databaseRoot, ignored -> new AtomicLong()).getAndIncrement();
        Files.createDirectories(databaseRoot);
        directory = databaseRoot.resolve(String.format("generation-%08d", generation));
        Files.createDirectory(directory);
        Properties setup = new Properties();
        setup.setProperty("engine", engine);
        setup.setProperty("generation", Long.toString(generation));
        setup.setProperty("database_name", db);
        setup.setProperty("seed", Long.toString(state.getState().getSeedValue()));
        setup.setProperty("native_oracle_class", oracleName);
        setup.setProperty("sqlancer_commit", "1d797ee53631df2fe574468e2be0425985f39867");
        int i = 0;
        for (var query : state.getState().getStatements()) {
            setup.setProperty("statement." + i++, query.getQueryString());
        }
        setup.setProperty("statement_count", Integer.toString(i));
        Path setupPath = directory.resolve("setup.xml");
        write(setupPath, setup);
        setupDigest = hex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(setupPath)));
        if (delegate.getClass() == NoRECOracle.class) {
            observeNoRECGenerator();
        }
    }

    @SuppressWarnings("unchecked")
    public static <G extends GlobalState<?, ?, ?>> TestOracle<G> wrap(G state, TestOracle<G> oracle,
            String engine) throws Exception {
        if (oracle.getClass() == CompositeTestOracle.class) {
            Field field = CompositeTestOracle.class.getDeclaredField("oracles");
            field.setAccessible(true);
            List<TestOracle<G>> children = (List<TestOracle<G>>) field.get(oracle);
            for (int i = 0; i < children.size(); i++) {
                children.set(i, wrap(state, children.get(i), engine));
            }
            return oracle; // Retain native scheduling, accounting and error behavior.
        }
        return new NativeObserver<>(state, oracle, engine);
    }

    private long logOffset() throws Exception {
        state.getLogger().getCurrentFileWriter().flush();
        return Files.size(nativeLog);
    }

    private byte[] logChunk(long start, long end) throws Exception {
        if (end < start) {
            throw new IllegalStateException("Native log reset within an oracle check");
        }
        byte[] bytes = new byte[Math.toIntExact(end - start)];
        try (RandomAccessFile input = new RandomAccessFile(nativeLog.toFile(), "r")) {
            input.seek(start);
            input.readFully(bytes);
        }
        return bytes;
    }

    private static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder();
        for (byte value : bytes) {
            out.append(String.format("%02x", value & 255));
        }
        return out.toString();
    }

    private static void write(Path path, Properties properties) throws Exception {
        try (OutputStream out = Files.newOutputStream(path, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            // Generated SQL can contain unpaired UTF-16 surrogates as well as NUL.
            // JDK writeChars preserves every original Java code unit without replacement.
            Properties transport = new Properties();
            transport.setProperty("_encoding", "base64-utf16be-codeunits");
            for (String key : properties.stringPropertyNames()) {
                ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                try (DataOutputStream data = new DataOutputStream(bytes)) {
                    data.writeChars(properties.getProperty(key));
                }
                transport.setProperty(key, Base64.getEncoder().encodeToString(bytes.toByteArray()));
            }
            transport.storeToXML(out, "Raw native oracle observation; truth label unassigned", "UTF-8");
        }
    }

    private void observeNoRECGenerator() throws Exception {
        Field field = NoRECOracle.class.getDeclaredField("gen");
        field.setAccessible(true);
        Object[] current = {field.get(delegate)};
        Object observer = Proxy.newProxyInstance(NoRECGenerator.class.getClassLoader(),
                new Class<?>[] {NoRECGenerator.class}, (proxy, method, args) -> {
                    Object result;
                    try {
                        result = method.invoke(current[0], args);
                    } catch (InvocationTargetException error) {
                        throw error.getCause();
                    }
                    if (method.getName().equals("setTablesAndColumns")) {
                        // Preserve the exact generator returned by the native implementation.
                        current[0] = result;
                        return proxy;
                    }
                    if (method.getName().equals("generateOptimizedQueryString")) {
                        generated.setProperty("optimized_sql", (String) result);
                        generated.setProperty("optimized_comparison", (Boolean) args[2] ? "extractCounts" : "countRows");
                    } else if (method.getName().equals("generateUnoptimizedQueryString")) {
                        generated.setProperty("unoptimized_sql", (String) result);
                        generated.setProperty("unoptimized_comparison", "extractCounts");
                    }
                    return result;
                });
        field.set(delegate, observer);
    }

    private void observeTLP(Properties record) throws Exception {
        Object reproducer = delegate.getLastReproducer();
        if (reproducer == null) {
            record.setProperty("complete_native_relation", "false");
            return;
        }
        // Fail explicitly if this pinned native representation changes.
        if (!reproducer.getClass().getSimpleName().equals("TLPWhereReproducer")) {
            record.setProperty("complete_native_relation", "false");
            record.setProperty("reproducer_class", reproducer.getClass().getName());
            return;
        }
        for (String name : new String[] {"originalQueryString", "firstQueryString", "secondQueryString",
                "thirdQueryString", "orderBy"}) {
            Field field = reproducer.getClass().getDeclaredField(name);
            field.setAccessible(true);
            record.setProperty(name, String.valueOf(field.get(reproducer)));
        }
        record.setProperty("complete_native_relation", "true");
        record.setProperty("native_comparison", "first_column_cardinality_and_native_set_comparator");
        record.setProperty("relation", "original_vs_three_partition_combination");
    }

    @Override
    public void check() throws Exception {
        long nativeStart = logOffset();
        generated.clear();
        long index = sequence++;
        String status = "unexpected_termination";
        String errorType = "";
        try {
            delegate.check();
            status = "native_oracle_passed";
        } catch (IgnoreMeException error) {
            status = "native_ignored";
            errorType = error.getClass().getName();
            throw error;
        } catch (Exception error) {
            status = "native_exception";
            errorType = error.getClass().getName();
            throw error;
        } catch (Error error) {
            status = "native_error";
            errorType = error.getClass().getName();
            throw error;
        } finally {
            Properties record = new Properties();
            record.putAll(generated);
            long nativeEnd = logOffset();
            byte[] nativeBytes = logChunk(nativeStart, nativeEnd);
            record.setProperty("native_log_path", nativeLog.toString());
            record.setProperty("native_log_start", Long.toString(nativeStart));
            record.setProperty("native_log_end", Long.toString(nativeEnd));
            record.setProperty("native_log_chunk_base64", Base64.getEncoder().encodeToString(nativeBytes));
            record.setProperty("native_log_chunk_sha256", hex(MessageDigest.getInstance("SHA-256").digest(nativeBytes)));
            record.setProperty("scope", "raw_native_oracle_observation");
            record.setProperty("truth_label", "unassigned");
            record.setProperty("native_oracle_class", oracleName);
            record.setProperty("sequence", Long.toString(index));
            record.setProperty("native_status", status);
            record.setProperty("error_type", errorType);
            record.setProperty("setup_sha256", setupDigest);
            if (delegate.getClass() == TLPWhereOracle.class) {
                observeTLP(record);
            } else if (delegate.getClass() == NoRECOracle.class) {
                record.setProperty("relation", "native_count_comparison");
                record.setProperty("complete_native_relation", Boolean.toString(
                        generated.containsKey("optimized_sql") && generated.containsKey("unoptimized_sql")));
            } else {
                record.setProperty("relation", "native_tlp_oracle_log");
                record.setProperty("complete_native_relation", "false");
            }
            write(directory.resolve(String.format("attempt-%08d.xml", index)), record);
        }
    }

    @Override
    public Reproducer<G> getLastReproducer() {
        return delegate.getLastReproducer();
    }

    @Override
    public String getLastQueryString() {
        return delegate.getLastQueryString();
    }
}
