Polygon
========

Symbolic Reasoning for SQL using Conflict-Driven Under-Approximation Search

----------------

In what follows, we provide a documentation for reproducing the results and facilitating the reusability of
the techniques presented in the paper "Polygon: Symbolic Reasoning for SQL using Conflict-Driven
Under-Approximation Search."

We begin with a **Getting Started Guide** (for Phase 1) that explains how to set up the Docker container and
run our tool on an example benchmark, followed by **Step-by-Step Instructions** (for Phase 2) for reproducing
the full results presented in the paper.

Finally, our detailed **Reusability Guide** documents how our tool can be integrated into and benefit other
research projects and how to extend it to support new features.

## Prerequisite

- Docker (version 27.5.1 or later)

----------------

## Getting Started Guide

In the root directory of the repository, run:

```shell
chmod +x start.sh && ./start.sh
```

(If you encounter a permission problem, please use sudo to execute ./start.sh.)

The script will build the Docker container and start an interactive shell session.

Then, in the Docker container, run:

```shell
python3 sql_testing/example.py
```

which will run Polygon on the motivating example presented in the paper's Overview section.
The expected output is similar to:

```
NEQ Time: 0.262586
[21:36:58] [INFO] Counterexample: {'customers': [['customer_id', 'customer_name', 'email'], [26, None, '37'], [25, None, '37']], 'contacts': [['user_id', 'contact_email', 'contact_name'], [25, '37', '47'], [26, '27', '35']], 'invoices': [['invoice_id', 'user_id', 'price'], [46, 25, 1]]}
[21:36:58] [INFO] Q1: [(46, None, 1, 1, Decimal('1'))]
[21:36:58] [INFO] Q2: [(46, None, 1, 2, 2)]
Refuted: True
```


----------------


## Step-by-Step Instructions

### RQ1

As our full evaluation contains a total of 31,175 benchmarks, running the full experiments may take a long
time to finish. For that reason, we provide a subset of our benchmarks consisting of approximately
800 benchmarks sampled from the ER dataset and 50 benchmarks each from the D-50 and D-100 datasets.
This smaller collection allows for preliminary validation of our results within a limited timeframe.


  - #### Option I. Subset (estimated running time: 1 hour)

In the Docker container, run:

```shell
bash run_rq1_subset.sh
```

The expected output on the subset of benchmarks is a table in CSV format as follows:

```
benchmark,solved,nodes_avg,nodes_med,nodes_max,time_avg,time_med,num_iters_avg,num_iters_med,num_iters_max,num_resolve_conflict_avg,num_resolve_conflict_med,num_resolve_conflict_max,adjusted_avg,adjusted_med,adjusted_max
ER,681,9,9,15,0.3,0.3,3.7,4.0,12,1.7,2.0,10,3,2,10
D-50,48,213,218,291,6.07,2.6,3.73,3.5,11.0,1.73,1.5,9.0,96,50,263
D-100,48,435,448,553,15.08,13.4,4.75,4.0,12.0,1.75,1.0,9.0,126,100,261
```


  - #### Option II. Full Experiment (estimated running time: 30 hours)

In the Docker container, run:

```shell
bash run_rq1_full.sh
```

Upon finish, the expected output for full experiments is a table in CSV format as follows, which would be
consistent with the results presented in Table 1:

```
benchmark,solved,nodes_avg,nodes_med,nodes_max,time_avg,time_med,num_iters_avg,num_iters_med,num_iters_max,num_resolve_conflict_avg,num_resolve_conflict_med,num_resolve_conflict_max,adjusted_avg,adjusted_med,adjusted_max
ER,5497,9,8,33,0.6,0.1,4.5,3.0,139,2.5,1.0,137,6,5,26
D-50,4004,211,213,343,6.36,3.7,4.81,4.0,39.0,1.82,1.0,36.0,82,51,280
D-100,2392,435,442,616,21.67,18.0,4.86,4.0,23.0,1.86,1.0,20.0,126,101,380
```

Please note that, results may vary depending on the hardware used, and running within Docker containers
typically causes some performance degradation.


### RQ2

In the Docker container, run

```shell
bash run_rq2.sh
```

Then, Figure 9 and Figure 10 will be generated and saved in the `/polygon/plt/` directory.
There will be totally 4 files corresponding to the figures in RQ2 in the paper:

```
/polygon/plt/RQ2-Fig-9-a.pdf
/polygon/plt/RQ2-Fig-9-b.pdf
/polygon/plt/RQ2-Fig-10-a.pdf
/polygon/plt/RQ2-Fig-10-b.pdf
```

To easily view the figures, you may run the following command in the **host** machine to transfer the files
from the container to the host machine and use any pdf viewer to open the files:

```shell
docker cp polygon://polygon/plt .
```


### RQ3

Given that we have included an extensive set of ablations with totally 10 different modified versions of
Polygon, where for many of these ablations, we expect the majority of the 31,175 benchmarks to time out
(i.e. to exhaust the 60-second time budget per benchmark), it would be extremely time-intensive to run
the full set of experiments even with the subset of benchmarks.

Therefore, we provide both the complete raw results from our latest experiments (allowing direct generation
of the figures for validation), and the full set of scripts necessary to reproduce the ablation study
results (RQ3) independently if desired.

  - #### Option I. Produce figures from raw results (within a minute)

In the Docker container, run:

```shell
bash run_rq3_figures.sh
```

  - #### Option II. Full Experiment (estimated running time: 500 hours)

In the Docker container, run:

```shell
bash run_rq3.sh
```

Running either option will generate the figures for RQ3 in the `/polygon/plt/`
directory.  Specifically, the following will be saved in the directory:

```
/polygon/plt/RQ3-Fig-11-a.pdf
/polygon/plt/RQ3-Fig-11-b.pdf
/polygon/plt/RQ3-Fig-12-a.pdf
/polygon/plt/RQ3-Fig-12-b.pdf
```

To transfer the files from the container to the host machine and view the figures,
you may run the following command in the **host** machine:

```shell
docker cp polygon://polygon/plt .
```

----------------

## Reusability Guide

In this section, we provide a detailed guide on how to run Polygon on new inputs, and how to extend Polygon
to support new features for further research.


- ### How to run Polygon on a new inputs?

Polygon's highly modularized structure makes it easy to integrate into other projects and adapt for new inputs.

In `sql_testing/example.py`, we provide an equivalence refutation example that showcases the interface of Polygon.

In what follows, we walk the reader through an example to explain the Polygon's interface and demonstrate how it
can be used with new inputs.

(1) **Specify the schema and integrity constraints**

Firstly, change the schema in `sql_testing/example.py` to the new schema.
Consider the following schema:

```
schema = [
    {
        "TableName": "Employees",
        "PKeys": [
            {
                "Name": "emp_id",
                "Type": "int"
            }
        ],
        "FKeys": [],
        "Others": [
            {
                "Name": "name",
                "Type": "varchar"
            },
            {
                "Name": "age",
                "Type": "int"
            }
        ]
    }
]
```

which corresponds to a table `Employees` with primary key `emp_id` and two
attributes `name` and `age`.

Then, specify `constraints` in `sql_testing/example.py` as follows:

```
constraints = [{'distinct': ['Employees.emp_id']}]
```

which means that the `emp_id` attribute in the `Employees` table should be distinct
(as it is a primary key).


(2) **Specify the SQL queries**

Next, specify the SQL queries in `sql_testing/example.py`.  For example,


```python
queries = [
    """
    SELECT emp_id FROM Employees WHERE age > 30
    """,

    """
    SELECT emp_id FROM Employees WHERE age >= 30
    """
]
```

The first query selects the `emp_id` from the `Employees` table where the `age` is greater than 30,
whereas the second query selects the `emp_id` from the `Employees` table where the `age` is greater than or equal to 30.

As we can see, the two queries are semantically not equivalent, and are expected to be refuted by Polygon.

(3) **Run Polygon on the new input**

Running Polygon on the new input as specified above by running `python3 sql_testing/example.py`, the user may expect an output similar to:

```
NEQ Time: 0.083584
[22:47:14] [INFO] Counterexample: {'employees': [['emp_id', 'name', 'age'], [7, '5', 31], [8, '6', 30]]}
[22:47:14] [INFO] Q1: [(7,)]
[22:47:14] [INFO] Q2: [(7,), (8,)]
Refuted: True
```

which means that the SQL queries are successfully refuted, with an input database (counterexample) shown above.
The output also shows the results of executing the SQL queries on the generated input.

The use of Polygon for the disambiguation application generally follows the same steps as above,
except that instead of `env.check`, `env.disambiguate` needs to be called and it takes
a list of queries as input. The output will be the same as in the equivalence
refutation application where if the set of provided queries can be disambiguated,
the output will be an input database.

- ### How to extend Polygon to support new application conditions?

As a general-purpose symbolic reasoning engine for SQL, Polygon is designed to be capable of generating inputs
that satisfy arbitrary conditions expressed in SMT.

To do so, the user needs to modify the `sql_testing/environment.py` file. In particular, the user will create
a new method similar to `check` or `disambiguate` in which SMT formula for the desired application condition
will be specified and conjoined by using `self.formulas.append()`.

For example, consider a scenario that a user wants to generate inputs satisfying the condition that the output
table of a query is non-empty, the user may write a formula that encodes there is at least one tuple in the
output table is *not deleted*, storing it in a variable, for instance, `non_empty_cond`, and then conjoin it
to the existing formulas by `self.formulas.append(non_empty_cond)`.


- ### How to extend Polygon to support new features?

The user may want to extend Polygon to support new features, such as SQL expressions.

Since Polygon has already supported all commonly used clause-level SQL operators, it would be more likely
that the user wants to extend Polygon to support new SQL *expressions*.

To do so, the user needs to modify the `sql_testing/visitors/expression_encoder.py` file.
The user may want to add a branch in the `visit_Expression` method to handle the new expression.

For example, the following code snippet shows how to handle the `DATE_ADD` expression in SQL:

```python
if node.operator == 'date_add':
    date_val, date_null = node.args[0].accept(self)
    days_to_add, _ = node.args[1].accept(self)
    return date_val + days_to_add, date_null
```

In particular, `date_val` and `date_null` stores the symbolic value and nullability of the first argument
of the `DATE_ADD` expression (the attribute). And `days_to_add` stores the how many days to add to the date.
Finally, the return value will be a tuple consisting of the returning symbolic value and nullability for the
expression.
