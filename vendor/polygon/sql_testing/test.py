import ast
import json
import logging
import os

from datetime import datetime

from sql_testing.testers.mysql_tester import MySQLTester
from sql_testing.testers.mysql_tester import DB_CONFIG
from sql_testing.constraint_parser import ConstraintParser
from sql_testing.environment import Environment
from sql_testing.logger import logger


def main():
    logger.setLevel(logging.DEBUG)

    benchmark_dir = os.path.join(os.path.dirname(__file__), '..', 'benchmarks')

    index = {}
    for line in open(os.path.join(benchmark_dir, 'cubes', 'all.jsonlines')):
        line = json.loads(line)
        index[f'{line["id"]}'] = line['schema']

    filename = 'spider_bak-network_2-0009.txt'
    queries = [
        """select t_person_0.age as age from person as t_person_0 where t_person_0.job = 'doctor' and t_person_0.name = 'zach'""",
        """select distinct t_cte_116.c_age0 as age from (select t_person_0.age as c_age0 from person as t_person_0 where 'doctor' = t_person_0.job) as t_cte_116"""
    ]

    schema = json.load(open(index[filename.replace('.txt', '').replace('-', '/')]))['Tables']
    print(schema)

    constraints = []

    env = Environment(schema, constraints, bound=3, time_budget=120)

    eq, cex, checking_time, total_time, ret = env.check(*queries)
    print(ret)
    if eq is None:
        print('ERR')
    else:
        if not eq:
            print('NEQ', checking_time, total_time)
            logger.info(cex)
            with MySQLTester(DB_CONFIG, schema) as tester:
                tester.create_all_databases([cex], constraints)
                rejected = tester.test_pair(*queries)
                print(rejected)
        else:
            print('EQ')

#     env.gen_db("""
#     SELECT memb_no, memb_name, publisher, COUNT(isbn)
# FROM (
#     SELECT member.memb_no as memb_no, name as memb_name, book.isbn as isbn, title, authors, publisher, date
#     FROM member INNER JOIN borrowed ON member.memb_no = borrowed.memb_no
#                 INNER JOIN book ON borrowed.isbn = book.isbn
# ) member_borrowed_book
# GROUP BY memb_no, memb_name, publisher
# HAVING COUNT(isbn) > 5""")
#     print(env.databases)

    # print(env.db)


if __name__ == '__main__':
    main()
