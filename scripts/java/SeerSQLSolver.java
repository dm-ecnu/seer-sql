import java.nio.file.Files;
import java.nio.file.Path;
import sqlsolver.api.entry.Verification;

/** I/O only: the official Verification implementation is unchanged. */
public final class SeerSQLSolver {
  public static void main(String[] args) throws Exception {
    if (args.length != 4) throw new IllegalArgumentException("expected four file paths");
    String result = Verification.verify(Files.readString(Path.of(args[0])),
        Files.readString(Path.of(args[1])), Files.readString(Path.of(args[2]))).toString();
    Files.writeString(Path.of(args[3]), result + "\n");
  }
}
