import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import sqlsolver.api.entry.Verification;

/** Persistent transport only; the released Verification implementation is unchanged. */
public final class SeerSQLSolverPersistent {
  public static void main(String[] args) throws Exception {
    if (args.length != 0) throw new IllegalArgumentException("no command-line arguments expected");
    BufferedReader requests = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
    String line;
    while ((line = requests.readLine()) != null) {
      String[] paths = line.split("\t", -1);
      if (paths.length != 4) throw new IllegalArgumentException("expected four tab-separated file paths");
      String answer;
      try {
        answer = Verification.verify(Files.readString(Path.of(paths[0])),
            Files.readString(Path.of(paths[1])), Files.readString(Path.of(paths[2]))).toString();
      } catch (Throwable error) {
        answer = "ERROR";
        error.printStackTrace(System.err);
      }
      Path destination = Path.of(paths[3]);
      Path partial = Path.of(paths[3] + ".partial");
      Files.writeString(partial, answer + "\n");
      Files.move(partial, destination, StandardCopyOption.REPLACE_EXISTING,
          StandardCopyOption.ATOMIC_MOVE);
    }
  }
}
