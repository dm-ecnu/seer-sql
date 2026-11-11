-- Composite foreign keys from the TPC-DS specification (v3.2.0, Section 2.3, column definitions of the returns
-- fact tables: "(sr_item_sk, sr_ticket_number) references store_sales (ss_item_sk, ss_ticket_number)" and the
-- catalog/web analogues). They are not in the kit's tools/tpcds_ri.sql, which lists only single-column FKs.
alter table store_returns add constraint sr_ss foreign key (sr_item_sk, sr_ticket_number) references store_sales (ss_item_sk, ss_ticket_number);
alter table catalog_returns add constraint cr_cs foreign key (cr_item_sk, cr_order_number) references catalog_sales (cs_item_sk, cs_order_number);
alter table web_returns add constraint wr_ws foreign key (wr_item_sk, wr_order_number) references web_sales (ws_item_sk, ws_order_number);
