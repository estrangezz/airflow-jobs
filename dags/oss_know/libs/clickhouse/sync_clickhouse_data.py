import json

import clickhouse_driver

from oss_know.libs.util.clickhouse_driver import CKServer
from oss_know.libs.util.log import logger


def union_remote_owner_repos(local_ck_conn_info, remote_ck_conn_info, table_name):
    remote_uniq_owner_repos_sql = f"""
    select distinct(search_key__owner, search_key__repo)
    from remote(
            '{remote_ck_conn_info["HOST"]}:{remote_ck_conn_info["PORT"]}',
            '{remote_ck_conn_info["DATABASE"]}.{table_name}',
            '{remote_ck_conn_info["USER"]}',
            '{remote_ck_conn_info["PASSWD"]}'
        )
    """

    uniq_owner_repos_sql = f"""
    select distinct(search_key__owner, search_key__repo)
    from gits;
    """
    ck_client = CKServer(host=local_ck_conn_info.get("HOST"),
                         port=local_ck_conn_info.get("PORT"),
                         user=local_ck_conn_info.get("USER"),
                         password=local_ck_conn_info.get("PASSWD"),
                         database=local_ck_conn_info.get("DATABASE"),
                         kwargs={
                             "connect_timeout": 200,
                             "send_receive_timeout": 6000,
                             "sync_request_timeout": 100,
                         })

    local_owner_repos = [tup[0] for tup in ck_client.execute_no_params(uniq_owner_repos_sql)]
    remote_owner_repos = [tup[0] for tup in ck_client.execute_no_params(remote_uniq_owner_repos_sql)]
    return set(local_owner_repos).union(set(remote_owner_repos))


def sync_from_remote_by_repos(local_ck_conn_info, remote_ck_conn_info, table_name, owner_repos):
    failed_owner_repos = []
    failure_info = {}  # Key: err.code, value: err.message
    for owner_repo_pair in owner_repos:
        owner, repo = owner_repo_pair
        try:
            sync_from_remote_by_repo(local_ck_conn_info, remote_ck_conn_info, table_name, owner, repo)
        except clickhouse_driver.errors.ServerException as e:
            logger.error(f"Failed to sync {owner}/{repo}: {e.code}")
            if e.code not in failure_info:
                failure_info[e.code] = e.message
            failed_owner_repos.append((owner, repo, e.code))

    if failed_owner_repos:
        logger.error(f"Failure messages: {json.dumps(failure_info, indent=2)}")
        raise Exception(f"Failed to sync {len(failed_owner_repos)} repos: {failed_owner_repos}")


def sync_from_remote_by_repo(local_ck_conn_info, remote_ck_conn_info, table_name, owner, repo):
    local_ck_client = CKServer(host=local_ck_conn_info.get("HOST"),
                               port=local_ck_conn_info.get("PORT"),
                               user=local_ck_conn_info.get("USER"),
                               password=local_ck_conn_info.get("PASSWD"),
                               database=local_ck_conn_info.get("DATABASE"),
                               kwargs={
                                   "connect_timeout": 200,
                                   "send_receive_timeout": 6000,
                                   "sync_request_timeout": 100,
                               })
    local_db = local_ck_conn_info.get("DATABASE")

    remote_host = remote_ck_conn_info.get('HOST')
    remote_port = remote_ck_conn_info.get('PORT')
    remote_user = remote_ck_conn_info.get('USER')
    remote_password = remote_ck_conn_info.get('PASSWD')
    remote_db = remote_ck_conn_info.get('DATABASE')

    local_latest_updated_at_sql = f"""
    select search_key__updated_at from {local_db}.{table_name}
    where search_key__owner = '{owner}' and search_key__repo = '{repo}'
    order by search_key__updated_at desc
    limit 1
    """

    cols = local_ck_client.execute_no_params(local_latest_updated_at_sql)

    local_latest_updated_at = 0 if not cols else cols[0][0]

    table_col_names_sql = f"""
    select distinct name
    from system.columns
    where database = '{local_db}'
      and table = '{table_name}'
    """
    cols = local_ck_client.execute_no_params(table_col_names_sql)
    cols_str = ",".join([col[0] for col in cols])

    insert_sql = f"""
    insert into table {local_db}.{table_name}
    select {cols_str}
    from remote(
            '{remote_host}:{remote_port}',
            '{remote_db}.{table_name}',
            '{remote_user}',
            '{remote_password}'
        )
       where search_key__owner = '{owner}'
       and search_key__repo = '{repo}'
        and search_key__updated_at > {local_latest_updated_at}
    """
    logger.info(f"Syncing {owner}/{repo}(updated_at > {local_latest_updated_at})")
    local_ck_client.execute_no_params(insert_sql)

    # Log for the inserted data
    new_insert_count_sql = f"""
    select count() from {local_db}.{table_name}
    where search_key__owner = '{owner}'
    and search_key__repo = '{repo}'
    and search_key__updated_at > {local_latest_updated_at}
    """
    result = local_ck_client.execute_no_params(new_insert_count_sql)
    new_insert_count = 0 if not result else result[0][0]
    logger.info(f"Synced {new_insert_count} rows for {owner}/{repo}(updated_at > {local_latest_updated_at})")
