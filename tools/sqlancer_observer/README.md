# Native SQLancer oracle observations

External ServiceLoader providers for SQLancer `1d797ee53631df2fe574468e2be0425985f39867`. The native provider still generates the database and executes its oracle. The extension records each `check()` boundary, original SQL, NoREC count mode, and TLP WHERE partition relation. Every observation has `truth_label=unassigned`.

The wrapper accepts native `NoRECOracle`, `TLPWhereOracle`, and the pinned SQLite/PostgreSQL TLP classes. A native `CompositeTestOracle` retains its original scheduler and accounting while its children are wrapped; other oracle classes fail explicitly. The pinned MySQL factory does not provide NoREC. SQLite execution, including its five-child `QUERY_PARTITIONING` composition, is covered by the recorded integration checks. A PostgreSQL 16 compatibility check verified the recorded SQL logs, while all default TLP attempts were ignored by the native tool. Successful-case coverage on the pinned SQLancer CI PostgreSQL 18 environment and MySQL execution remain to be verified.

Build against a successful original `mvn package -DskipTests` result on the assigned server CPU:

```sh
python build.py --sqlancer-jar /absolute/sqlancer/target/sqlancer-2.0.0.jar \
  --native-build-result /absolute/native-build/result.json \
  --jdk /absolute/jdk17 --output /absolute/fresh/observer-build
```

The build binds the native source commit, successful build receipt, jar and runtime file hashes. It leaves native source and dependency versions unchanged. The output is `seer-sqlancer-observer.jar`.

Run the native entrypoint with the extension on its classpath, using the corresponding external command (`seer-sqlite3`, `seer-postgres`, or `seer-mysql`):

```sh
java -Dseer.sqlancer.observations=/absolute/fresh/observations \
  -cp '/absolute/observer-build/seer-sqlancer-observer.jar:/absolute/sqlancer/target/sqlancer-2.0.0.jar:/absolute/sqlancer/target/lib/*' \
  sqlancer.Main --num-threads 1 --num-tries 1 --max-generated-databases 1 \
  --num-queries 20 --random-seed 0 seer-sqlite3 --oracle NoREC
```

These query counts and seeds illustrate a development check, not a benchmark protocol. Preserve the workload's agreed settings for actual data collection.

Each native child-oracle construction has its own generation directory, containing `setup.xml` and immutable numbered attempt files linked to the setup SHA256. Field values use Base64 of the original Java UTF-16BE code units (`_encoding=base64-utf16be-codeunits`). This preserves NUL and unpaired surrogates that XML and UTF-8 conversion cannot preserve directly. Import `read_records.read_record(path)` to decode, or use `python read_records.py RECORD.xml` for escaped JSON. No query normalization or equivalence labels are produced here.

Each attempt also stores the unchanged native logger byte interval, its SHA256 and its Base64 bytes. This captures the queries actually logged by a child check without parsing SQL or guessing its relation. NoREC and TLP WHERE retain their structured fields. Other TLP classes currently have `complete_native_relation=false`; their raw log is preserved, and structured relation export remains unfinished. Per-select logging must remain enabled (the native default).
