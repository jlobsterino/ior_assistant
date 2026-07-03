# -*- coding: utf-8 -*-
"""
Расчёт потерь по кредиту: невозможность взыскания задолженности.

Параметризованная версия (без интерактивного input - все параметры в PARAMS).
Формулы расчёта РВПС/залога/коэффициентов сохранены без изменений.
Источники — кредитные витрины (b2c_credit_f1, cred_yul, 
custom_risk_kf1_v_relevant_loans_saldo, cards). Требует проверки на
реальных данных перед боевым использованием.

Этот файл — единый источник логики; ноутбук собирается из него скриптом
scripts/build_credit_notebook.py (py_compile-проверка проходит на этом .py).
"""

from decimal import Decimal
import pandas as pd
from datetime import datetime, timedelta

# ВНЕ DataLab переменные spark/PARAMS инжектятся в ноутбуке. Здесь
# объявлены как None только чтобы файл проходил py_compile.
spark = None
PARAMS = {}
DM = "arnsdpsbx_t_team_sva_oarb_4."

# --- Справочники
digital_risk_profile = {
    'DRP-10101': 'Невзимание средств в погашение задолженности по кредитному продукту',
    'DRP-10023': 'Вывод средств из бизнеса заемщиком',
    'DRP-10047': 'Отсутствие возможности взыскания задолженности по кредитному продукту',
    'DRP-10024': 'Выдача кредита заемщику, несоответствующему требованиям Банка',
    'DRP-10138': 'Некорректное списание средств',
    'DRP-10027': 'Неоформление / утрата обеспечения',
    'DRP-10192': 'Снижение категории качества ссуды по фактору недостоверная отчетность',
    'DRP-10196': 'Выдача кредита заемщику, вовлеченному в финансовую пирамиду',
    'DRP-10100': 'Выдача кредита заемщику под залог имущества, не соответствующего требованиям кредитного продукта',
    'DRP-10025': 'Неоформление предмета залога',
    'DRP-10102': 'Хищение средств со счетов третьими лицами',
    'DRP-10103': 'Хищение средств со счетов сотрудниками'
}

deviation_type = {
    '1': 'Внешнее/внутреннее мошенничество',
    '2': 'Фиктивные данные с целью получения кредита',
    '3': 'Умышленное предоставление Заемщиком недостоверных данных/информации с целью получения кредитных средств банка на сроке кредитования, выявленных после заключения КОД',
    '4': 'Умышленное предоставление сотрудниками Банка недостоверной информации, на основании которой было принято решение о кредитовании/внесении изменений в условия кредитования',
    '5': 'Некорректное принятие кредитного решения по заявке (кредита, кредитных карт)',
    '6': 'Несоблюдение условий кредитного продукта (кредита, кредитной карты)',
    '7': 'Ошибки сотрудников Банка при рассмотрении, в процессе выдачи и сопровождении/мониторинге кредита и кредитных карт',
    '8': 'Выдача кредита недееспособному (дата признания клиента недееспособным предшествует дате выдачи кредита)',
    '9': 'Социальная инженерия',
    '10': 'На этапе рассмотрения кредитной заявки не выявлено завышение стоимости предмета залога',
    '11': 'Не полные, не точные, не актуальные данные',
    '12': 'Некорректное функционирование систем',
    '13': 'Недоступность АС/ФП',
    '14': 'Некорректный анализ сделки',
    '15': 'Недостатки процесса выявления и проставления маркера в профиле клиента',
    '16': 'Компрометация банковской карты умершего клиента третьими лицами',
    '17': 'Несвоевременное внесение изменений в АС',
    '18': 'Проблемы на уровне бизнес-требований',
    '19': 'Отсутствие технологических изменений/доработок',
    '20': 'Проблемы связанные с внедрением релиза или технологического изменения',
    '21': 'Недостатки при разработке',
    '22': 'Внедрение непролидированных ML/AI Моделей',
    '23': 'Проведение операций по кредитной карте клиента без ведома клиента',
    '24': 'Получение кредита/кредитной карты за клиента третьими лицами с использованием кодов доступа, в результате утраты телефонов клиентом, компрометации доступов в МБ/СБОЛ клиентом, после смерти клиента (соц. инженерия)',
    '26': 'Проведение расходных операций по кредитным картам / увеличение лимита кредитной карты после смерти клиента',
    '27': 'Несанкционированная клиентом выдача кредитной карты по предодобренным спискам с последующим хищением сотрудником',
    '28': 'Вывод обеспечения из залога без согласия Банка / без принятия коллегиальным органом Банка соответствующего решения',
    '29': 'Реализация Заемщиком предмета залога без согласия Банка',
    '30': 'Физическая утрата (снос, пожар, разрушение, кража и т.д.) предмета залога',
    '31': 'Не заключение соответствующих дополнительных соглашений к обеспечительной документации при изменении существенных условий кредитования (сумма, срок, процентная ставка)',
    '32': 'Внесение несогласованных с Гарантами/Поручителями изменений в кредитную документацию, прекращение прав собственности Залогодателя на предмет залога по решению суда, ошибочное снятие обременения с предмета залога в уполномоченных органах, допущены ошибки при оформлении КОД',
    '33': 'Не оформление обеспечения, предусмотренного кредитной документацией / условиями кредитования Заемщика, утвержденными коллегиальным органом Банка',
    '34': 'Выдача кредитов ЮЛ, заключение сделок сверх максимально возможного лимита кредитного риска',
    '35': 'Предоставлен фиктивный отчет об оценке залогового имущества',
    '36': 'Прочее',
    '37': 'События, связанные со сбоями и ошибками работы АС/витрин/стратегий принятия решений / сотрудников при увеличении РВПС с даты выдачи кредита',
    '38': 'Случаи внешнего мошенничества',
    '39': 'Выдача кредитов ЮЛ, заключение сделок сверх максимально возможного лимита кредитного риска',
    '40': 'Предоставлен фиктивный отчет об оценке залогового имущества',
    '41': 'Несоблюдение отлагательных условий, блокирующих факторов',
    '42': 'Признание сделки недействительной вследствие умышленных действий участника сделки',
    '43': 'Выдача кредитов ЮЛ, заключение сделок сверх максимально возможного лимита кредитного риска',
    '44': 'Преднамеренное банкротство',
    '45': 'Утрата кредитно-обеспечительной документации',
    '46': 'Решением суда кредитный договор признан недействительным'
}

client_type = {'1': 'ФЛ', '2': 'КСБ', '3': 'ММБ'}
DRP_10027_type = {'1': 'Утрата обеспечения', '2': 'Неоформление обеспечения'}

factor_op = {
    '1': 'Признаки оттока капитала',
    '2': 'Признаки подозрения на фальсификацию и/или искажение предоставленных документов',
    '3': 'Снижение/утрата источников погашения',
    '4': 'Наличие негативной информации, которая может отрицательно повлиять на бизнес Контрагента/Участника сделки'
}

weights = {'1': 30, '2': 18, '3': 18, '4': 3}
bankrot = '1'

data_coef = {
    'bancrot_type': ['0', '0', '0', '0', '0', '1', '1', '1', '1', '1'],
    'credit_type': ['1', '2', '3', '4', '5', '6', '1', '2', '3', '4', '5', '6'],
    'coefval': ['49', '45', '20', '15', '20', '24', '49', '15', '4', '5', '7', '6']
}
coef = pd.DataFrame(data_coef)

# Глобалы, которые расчетные функции читают (устанавливаются в main из PARAMS)
selected_risk = None
selected_deviation = None
selected_borrower = None
incident_date = None
selected_DRP_10027 = None

# --- Параметризованный выбор факторов (вместо интерактивного input)
def select_factors_from_params(factor_codes):
    if not factor_codes:
        return 0
    return sum(weights[k] for k in factor_codes if k in weights)

# --- Получение данных по кредиту ФЛ
def get_loan_data(spark, id_credit):
    try:
        sql = f"""
        SELECT loan_agrmnt_id, etsm_request_id, issue_dt, issue_rub_amt, epk_id,
            CASE WHEN agrmnt_status_name = 'Снят с баланса' THEN '1' ELSE '0' END vne_balance
        FROM {DM}d6_base_of_knowledge_b2c_credit_f1
        WHERE loan_agrmnt_id = {id_credit}
        """
        df_spark = spark.sql(sql)
        if df_spark.count() == 0:
            print(f"Данные для кредита {id_credit} не найдены")
            return None
        return df_spark.toPandas()
    except Exception as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return None

def get_zalog_data(spark, id_credit):
    try:
        sql = f"select * from {DM}d6_base_of_knowledge_b2c_credit_f1 WHERE loan_agrmnt_id = {id_credit}"
        df_spark = spark.sql(sql)
        if df_spark.count() == 0:
            print(f"Данные для залога по кредиту {id_credit} не найдены")
            return None
        return df_spark.toPandas()
    except Exception as e:
        print(f"Ошибка при запросе по залогу: {e}")
        return None

# - Залог по кредиту ЮЛ (запрос к cred_yul_new_corr; фильтр host_agr_cred_id)
def get_zalog_jur_data(spark, id_credit, gregor_dt):
    try:
        sql = f"""
        select obj_plg_sid, OBJ_PLG_COLLATERAL_COST_UPD_RUB_AMT, agr_cred_id
        from {DM}d6_base_of_knowledge_cred_yul_new_corr
        where gregor_dt = '{gregor_dt}' and host_agr_cred_id = '{id_credit}'
        """
        df_spark = spark.sql(sql)
        if df_spark.count() == 0:
            print(f"Данные для залога юр.лица по кредиту {id_credit} не найдены")
            return None
        return df_spark.toPandas()
    except Exception as e:
        print(f"Ошибка при запросе по залогу: {e}")
        return None

def get_incident_data(spark, id_credit, incident_date):
    if not incident_date:
        print("Не указана дата обнаружения инцидента")
        return None
    try:
        sql = f"""
        select * FROM {DM}d6_base_of_knowledge_custom_risk_kf1_v_relevant_loans_saldo
        WHERE id_loan_sys = {id_credit} AND cast(day as date) = '{incident_date}'
        """
        return spark.sql(sql).toPandas()
    except Exception as e:
        print(f"Ошибка при запросе резерва на дату инцидента: {e}")
        return None

def get_reserve_data(spark, id_credit, issue_date):
    try:
        sql = f"""
        SELECT * FROM {DM}d6_base_of_knowledge_custom_risk_kf1_v_relevant_loans_saldo
        WHERE id_loan_sys = {id_credit} AND cast(day as date) = '{issue_date}' + interval '1' DAY
        """
        return spark.sql(sql).toPandas()
    except Exception as e:
        print(f"Ошибка при получении резерва на дату выдачи: {e}")
        return None

# - Основная информация по кредиту ЮЛ (СКОРРЕКТИРОВАНО: без src_sys_id)
def get_loan_juridical_face_data(spark, id_credit, gregor_dt):
    try:
        sql = f"""
        select issue_dt, initial_f26_rub, org_epk_sid, debt_amt, debt_ovr_rub,
            prvsn_debt_rub, INTRST_DUE_RUB, INTRST_OVR_RUB, agr_cred_stts_type_name,
            debt_prvsn_write_off_rub, gregor_dt
        from {DM}d6_base_of_knowledge_cred_yul_new_corr
        where host_agr_cred_id = '{id_credit}' and gregor_dt = '{gregor_dt}'
        """
        df_spark = spark.sql(sql)
        if df_spark.count() == 0:
            print(f"Данные для кредита {id_credit} не найдены")
            return None
        return df_spark.toPandas()
    except Exception as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return None

def get_coef_rtk(in_balance, is_bankrot, product):
    try:
        apply_coef = -1
        if in_balance == '1':
            apply_coef = 3
        else:
            apply_coef = coef.loc[(coef['bancrot_type'] == is_bankrot)
                                  & (coef['credit_type'] == product)]['coefval'].iloc[0]
        if apply_coef == -1:
            raise Exception('Коэффициент не найден')
        return Decimal(Decimal(apply_coef) / Decimal(100.0))
    except Exception:
        print("Процесс прерван (коэффициент).")
        return 0

def get_incident_cc_data_rtk(spark, id_contract, incident_date):
    if not incident_date:
        return None
    try:
        sql = f"""
        SELECT contract_idt, contract_number, vne_balance,
            COALESCE(ln_int_amount, 0) ln_int_amount,
            COALESCE(loan_amount, 0) loan_amount,
            COALESCE(pd_amount, 0) pd_amount,
            COALESCE(fee_ovd_amount, 0) fee_ovd_amount,
            COALESCE(ovd_amount, 0) ovd_amount,
            COALESCE(fee_ovd_amount, 0) fee_ovd_amount,
            COALESCE(ovd_amount, 0) ovd_amount,
            COALESCE(ovd_int_amount, 0) ovd_int_amount,
            COALESCE(penalty_amount, 0) penalty_amount
        FROM {DM}d6_base_of_knowledge_cards
        WHERE contract_number = '{id_contract}'
        """
        return spark.sql(sql).toPandas()
    except Exception as e:
        print(f"Ошибка при запросе остатка КК: {e}")
        return None

def get_incident_data_rtk(spark, id_credit, incident_date):
    if not incident_date:
        return None
    try:
        sql_main = f"""
        SELECT id_loan_sys, in_saldo_debt_rur, in_saldo_delayed_debt_rur,
            in_saldo_delayed_debt_prc_rur, in_saldo_debt_prc_rur
        FROM {DM}d6_base_of_knowledge_custom_risk_kf1_v_relevant_loans_saldo
        WHERE id_loan_sys = {id_credit} AND CAST(DAY AS DATE) = '{incident_date}'
        """
        main_pd_df = spark.sql(sql_main).toPandas()
        sql_peny = f"""
        SELECT loan_agrmnt_id,
            COALESCE(remaining_peny_debt_rub_amt, CAST(0 AS DECIMAL(18, 2))) remaining_peny_debt_rub_amt,
            CASE WHEN agrmnt_status_name = 'Снят с баланса' THEN '1' ELSE '0' END vne_balance
        FROM {DM}d6_base_of_knowledge_b2c_credit_f1
        WHERE loan_agrmnt_id = {id_credit}
        """
        peny_pd_df = spark.sql(sql_peny).toPandas()
        return pd.merge(main_pd_df, peny_pd_df, left_on='id_loan_sys', 
                        right_on='loan_agrmnt_id', how='left')
    except Exception as e:
        print(f"Ошибка при запросе остатка кредита: {e}")
        return None

def get_credit_product(spark, id_credit):
    try:
        sql = f"""
        SELECT loan_agrmnt_id income_value, msfo9pr_code, '' AS contract_number,
            loan_agrmnt_id AS seacrh_id
        FROM {DM}d6_base_of_knowledge_b2c_credit_f1 WHERE loan_agrmnt_id = {id_credit}
        UNION
        SELECT CAST(_c0 AS BIGINT), 'KK', contract_number, contract_idt
        FROM {DM}d6_base_of_knowledge_risk_parsed_rsk_reqst WHERE _c0 = {id_credit}
        """
        df_spark = spark.sql(sql)
        if df_spark.count() == 0:
            return None
        return df_spark.toPandas()
    except Exception as e:
        print(f"Ошибка при получении типа продукта: {e}")
        return None

# --- Расчёт ФЛ (формулы дословно)
def physical_face_calculation(spark, id_credit):
    loan_df = get_loan_data(spark, id_credit)
    if loan_df is None or loan_df.empty:
        print("Не удалось получить данные кредита.")
        return None
    try:
        issue_dt = loan_df['issue_dt'].iloc[0].strftime('%Y-%m-%d')
        issue_rub_amt = loan_df['issue_rub_amt'].iloc[0]
        vne_balance = loan_df['vne_balance'].iloc[0]
    except (IndexError, KeyError) as e:
        print(f"Не удалось извлечь дату выдачи кредита: {e}")
        return None

    reserve_df = get_reserve_data(spark, id_credit, issue_dt)
    if reserve_df is None:
        print("Не удалось получить резерв на дату выдачи.")
        return None
    incident_df = get_incident_data(spark, id_credit, incident_date)

    loan_issuance_reserve = reserve_df['reserve_requirement'].iloc[0]
    loan_issuance_in_saldo_debt_rur = reserve_df['in_saldo_debt_rur'].iloc[0]
    loan_issuance_in_saldo_delayed_debt_rur = reserve_df['in_saldo_delayed_debt_rur'].iloc[0]
    loan_issuance_in_saldo_delayed_debt_prc_rur = reserve_df['in_saldo_delayed_debt_prc_rur'].iloc[0]
    loan_issuance_in_saldo_debt_prc_rur = reserve_df['in_saldo_debt_prc_rur'].iloc[0]
    loan_incident_reserve = incident_df['reserve_requirement'].iloc[0]
    in_saldo_debt_rur = incident_df['in_saldo_debt_rur'].iloc[0]
    in_saldo_delayed_debt_rur = incident_df['in_saldo_delayed_debt_rur'].iloc[0]
    in_saldo_delayed_debt_prc_rur = incident_df['in_saldo_delayed_debt_prc_rur'].iloc[0]
    in_saldo_debt_prc_rur = incident_df['in_saldo_debt_rur'].iloc[0]

    if ((loan_issuance_reserve < loan_incident_reserve) and 
        (((selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка' 
           and selected_deviation == 'Внешнее/внутреннее мошенничество') or
          selected_risk == 'Хищение средств со счетов третьими лицами' or
          selected_risk == 'Хищение средств со счетов сотрудниками')
         and selected_borrower == 'ФЛ')):
        direct_loss = (in_saldo_debt_rur + in_saldo_delayed_debt_rur) * Decimal(loan_incident_reserve)
        indirect_loss = (in_saldo_delayed_debt_prc_rur + in_saldo_debt_prc_rur) * Decimal(loan_incident_reserve)
        print(f"Прямые потери {direct_loss}, косвенные потери {indirect_loss}")
        return direct_loss, indirect_loss
    else:
        potential_loss = issue_rub_amt
        print(f"Потенциальные потери {potential_loss}")
        return potential_loss

# --- Расчёт ЮЛ ММБ (формулы дословно; input -> PARAMS)
def juridical_face_calculation(spark, id_credit, incident_date, selected_risk, selected_deviation):
    zalog_sum_amt = None
    selected_DRP_10027 = None
    loan_df_na_datu_incidenta = get_loan_juridical_face_data(spark, id_credit, incident_date)
    if loan_df_na_datu_incidenta is None or loan_df_na_datu_incidenta.empty:
        print("Не удалось получить данные кредита.")
        return None
    try:
        issue_dt = loan_df_na_datu_incidenta['issue_dt'].iloc[0].strftime('%Y-%m-%d')
        proc_reserv_na_datu_incidenta = loan_df_na_datu_incidenta['prvsn_debt_rub'].iloc[0]
        osz_na_datu_incidenta = loan_df_na_datu_incidenta['debt_amt'].iloc[0]
        pz_na_datu_incidenta = loan_df_na_datu_incidenta['debt_ovr_rub'].iloc[0]
        zadolzh_po_sroch_proc = loan_df_na_datu_incidenta['INTRST_DUE_RUB'].iloc[0]
        zadolzh_po_prosroch_proc = loan_df_na_datu_incidenta['INTRST_OVR_RUB'].iloc[0]
        summa_kredita = loan_df_na_datu_incidenta['initial_f26_rub'].iloc[0]
    except (IndexError, KeyError) as e:
        print(f"Не удалось извлечь дату выдачи кредита: {e}")
        return None

    from datetime import datetime, timedelta
    next_day = (datetime.strptime(issue_dt, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    loan_df_na_datu_vidachi = get_loan_juridical_face_data(spark, id_credit, next_day)
    proc_reserv_na_datu_vidachi = loan_df_na_datu_vidachi['prvsn_debt_rub'].iloc[0]
    delta_rvps = proc_reserv_na_datu_incidenta - proc_reserv_na_datu_vidachi
    rvps = (Decimal(osz_na_datu_incidenta) + Decimal(pz_na_datu_incidenta)) * Decimal(proc_reserv_na_datu_incidenta)
    rvp = (Decimal(zadolzh_po_sroch_proc) + Decimal(zadolzh_po_prosroch_proc)) * Decimal(proc_reserv_na_datu_incidenta)
    delta_rvps_sum = (Decimal(osz_na_datu_incidenta) + Decimal(pz_na_datu_incidenta)) * Decimal(delta_rvps)
    delta_rvp_sum = (Decimal(zadolzh_po_sroch_proc) + Decimal(zadolzh_po_prosroch_proc)) * Decimal(delta_rvps)

    if (delta_rvps > 0 and selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка' 
        and selected_deviation == 'События, связанные со сбоями и ошибками работы АС/витрин/стратегий принятия решений / сотрудников при увеличении РВПС с даты выдачи кредита'):
        return rvps * Decimal('0.75'), rvp * Decimal('0.75')
    elif (delta_rvps > 0 and selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка'
          and selected_deviation == 'Случаи внешнего мошенничества'):
        return rvps * Decimal('0.87'), rvp * Decimal('0.87')
    elif (delta_rvps <= 0 and selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка'
          and selected_deviation == 'Прочее'):
        return summa_kredita
    elif (delta_rvps <= 0 and selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка'
          and selected_deviation == 'Предоставлен фиктивный отчет об оценке залогового имущества'):
        amount_zalog_overact = str(PARAMS.get('zalog_overact_amount') or '')
        if amount_zalog_overact == '' or not amount_zalog_overact.isdigit():
            zalog_sum_amt = amount_zalog_overact
        return zalog_sum_amt
    elif (delta_rvps > 0 and selected_risk == 'Вывод средств из бизнеса заемщиком'):
        koef_vivod_sredstv = str(PARAMS.get('vivod_sredstv_pct') or '0')
        if int(koef_vivod_sredstv) <= 50:
            return rvps * Decimal(koef_vivod_sredstv), rvp * Decimal(koef_vivod_sredstv)
        else:
            return rvps * Decimal('0.87'), rvp * Decimal('0.87')
    elif (delta_rvps <= 0 and selected_risk == 'Вывод средств из бизнеса заемщиком'):
        summa_vivod_sredstv = str(PARAMS.get('vivod_sredstv_amount') or '0')
        if int(summa_vivod_sredstv) >= (osz_na_datu_incidenta + pz_na_datu_incidenta):
            return osz_na_datu_incidenta + pz_na_datu_incidenta
        else:
            return summa_vivod_sredstv
    elif (delta_rvps > 0 and selected_risk == 'Отсутствие возможности взыскания задолженности по кредитному продукту'):
        direct_loss = (Decimal(osz_na_datu_incidenta) + Decimal(pz_na_datu_incidenta)) * Decimal(delta_rvps)
        indirect_loss = (Decimal(zadolzh_po_sroch_proc) + Decimal(zadolzh_po_prosroch_proc)) * Decimal(delta_rvps)
        return direct_loss, indirect_loss
    elif (delta_rvps <= 0 and selected_risk == 'Отсутствие возможности взыскания задолженности по кредитному продукту'):
        return summa_kredita
    elif (selected_risk == 'Неоформление / утрата обеспечения'):
        selected_DRP_10027 = DRP_10027_type.get(str(PARAMS.get('drp_10027_type') or ''))
        zalog_sum_amt = get_zalog_jur_data(spark, id_credit, incident_date)
        if selected_DRP_10027 == 'Утрата обеспечения' and delta_rvps > 0:
            if delta_rvps_sum < zalog_sum_amt:
                return delta_rvps_sum, zalog_sum_amt - delta_rvps_sum
            else:
                return zalog_sum_amt
        if selected_DRP_10027 == 'Утрата обеспечения' and delta_rvps <= 0:
            return zalog_sum_amt
    return None

# --- Расчёт ЮЛ КСБ (формулы дословно; input -> PARAMS; баг порядка устранён) ---
def juridical_face_ksb_calculation(spark, id_credit, incident_date, selected_risk, selected_deviation):
    selected_DRP_10027 = None
    loan_df_na_datu_incidenta = get_loan_juridical_face_data(spark, id_credit, incident_date)
    if loan_df_na_datu_incidenta is None or loan_df_na_datu_incidenta.empty:
        print("Не удалось получить данные кредита.")
        return None

    # СНАЧАЛА получаем данные на дату выдачи (раньше использовались до присвоения - баг устранён)
    try:
        issue_dt = loan_df_na_datu_incidenta['issue_dt'].iloc[0].strftime('%Y-%m-%d')
    except (IndexError, KeyError) as e:
        print(f"Не удалось извлечь дату выдачи кредита: {e}")
        return None

    from datetime import datetime, timedelta
    next_day = (datetime.strptime(issue_dt, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    loan_df_na_datu_vidachi = get_loan_juridical_face_data(spark, id_credit, next_day)
    if loan_df_na_datu_vidachi is None or loan_df_na_datu_vidachi.empty:
        print("Не удалось получить данные на дату выдачи.")
        return None

    try:
        proc_reserv_na_datu_incidenta = loan_df_na_datu_incidenta['prvsn_debt_rub'].iloc[0]
        osz_na_datu_incidenta = loan_df_na_datu_incidenta['debt_amt'].iloc[0]
        pz_na_datu_incidenta = loan_df_na_datu_incidenta['debt_ovr_rub'].iloc[0]
        zadolzh_po_sroch_proc = loan_df_na_datu_incidenta['INTRST_DUE_RUB'].iloc[0]
        zadolzh_po_prosroch_proc = loan_df_na_datu_incidenta['INTRST_OVR_RUB'].iloc[0]
        summa_kredita = loan_df_na_datu_incidenta['initial_f26_rub'].iloc[0]
        summa_spis_za_shet_rezerva = loan_df_na_datu_vidachi['debt_prvsn_write_off_rub'].iloc[0]
    except (IndexError, KeyError) as e:
        print(f"Не удалось извлечь данные кредита: {e}")
        return None

    proc_reserv_na_datu_vidachi = loan_df_na_datu_vidachi['prvsn_debt_rub'].iloc[0]
    delta_rvps = proc_reserv_na_datu_incidenta - proc_reserv_na_datu_vidachi
    rvps = (Decimal(osz_na_datu_incidenta) + Decimal(pz_na_datu_incidenta)) * Decimal(proc_reserv_na_datu_incidenta)
    rvp = (Decimal(zadolzh_po_sroch_proc) + Decimal(zadolzh_po_prosroch_proc)) * Decimal(proc_reserv_na_datu_incidenta)
    delta_rvps_sum = (Decimal(osz_na_datu_incidenta) + Decimal(pz_na_datu_incidenta)) * Decimal(delta_rvps)
    delta_rvp_sum = (Decimal(zadolzh_po_sroch_proc) + Decimal(zadolzh_po_prosroch_proc)) * Decimal(delta_rvps)

    if (selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка' 
        and selected_deviation in [deviation_type['5'], deviation_type['6'], 
                                   deviation_type['14'], deviation_type['41'], 
                                   deviation_type['36']]):
        if delta_rvps > 0:
            return rvps * Decimal('0.89'), rvp * Decimal('0.89')
        else:
            summa_osz = summa_kredita * Decimal('0.89')
            print(f"Сумма ОСЗ/лимита на дату обнаружения {summa_osz}")
            return summa_osz

    elif (selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка' 
          and selected_deviation in [deviation_type['2'], deviation_type['42']]):
        koef = select_factors_from_params(PARAMS.get('factor_codes') or [])
        if delta_rvps > 0:
            return rvps * koef, rvp * koef
        else:
            return summa_kredita * koef

    elif (selected_risk == 'Выдача кредита заемщику, несоответствующему требованиям Банка' 
          and selected_deviation == deviation_type['40']):
        amount_zalog_overact = PARAMS.get('zalog_overact_amount')
        if amount_zalog_overact is None:
            return None
        zalog_sum_amt = Decimal(str(amount_zalog_overact))
        if delta_rvps > 0 and delta_rvps_sum < zalog_sum_amt:
            return delta_rvps_sum, zalog_sum_amt - delta_rvps_sum
        elif delta_rvps > 0 and delta_rvps_sum > zalog_sum_amt:
            return zalog_sum_amt
        elif delta_rvps <= 0:
            return zalog_sum_amt

    elif (selected_risk == 'Вывод средств из бизнеса заемщиком'):
        koef = select_factors_from_params(PARAMS.get('factor_codes') or [])
        if delta_rvps > 0:
            return rvps * koef, rvp * koef
        else:
            return summa_kredita * koef

    elif (selected_risk == 'Отсутствие возможности взыскания задолженности по кредитному продукту' 
          and selected_deviation == deviation_type['44']):
        koef = select_factors_from_params(PARAMS.get('factor_codes') or [])
        if delta_rvps > 0:
            return rvps * koef, rvp * koef
        else:
            return summa_kredita * koef

    elif (selected_risk == 'Отсутствие возможности взыскания задолженности по кредитному продукту' 
          and selected_deviation in [deviation_type['45'], deviation_type['46']]):
        if delta_rvps > 0:
            return delta_rvps_sum, delta_rvp_sum
        else:
            return summa_kredita

    elif (selected_risk == 'Неоформление / утрата обеспечения'):
        selected_DRP_10027 = DRP_10027_type.get(str(PARAMS.get('drp_10027_type') or ''))
        if selected_DRP_10027 == 'Утрата обеспечения':
            zalog_df = get_zalog_jur_data(spark, id_credit, incident_date)
            if zalog_df is None or zalog_df.empty:
                return None
            zalog = zalog_df['OBJ_PLG_COLLATERAL_COST_UPD_RUB_AMT'].iloc[0]
            if delta_rvps > 0 and delta_rvps_sum < zalog:
                return delta_rvps_sum, zalog
    return None

# --- main: читает PARAMS вместо input()
def main(spark, P):
    global selected_risk, selected_deviation, selected_borrower, incident_date
    selected_borrower = client_type.get(P.get('client_type'), P.get('client_type'))
    id_credit = P['id_credit']
    incident_date = P.get('incident_date')
    selected_risk = digital_risk_profile.get(P['risk_profile_code'], P['risk_profile_code'])
    selected_deviation = deviation_type.get(P['deviation_code'], P['deviation_code'])

    print("=" * 60)
    print("РАСЧЁТ ПОТЕРЬ ПО КРЕДИТУ")
    print("=" * 60)
    print(f"Клиент: {selected_borrower} | Кредит: {id_credit}")
    print(f"ЦПР: {selected_risk}")
    print(f"Отклонение: {selected_deviation}")

    result = None
    if selected_borrower == 'ФЛ':
        result = physical_face_calculation(spark, id_credit)
    elif selected_borrower == 'ММБ':
        result = juridical_face_calculation(spark, id_credit, incident_date, selected_risk, selected_deviation)
    elif selected_borrower == 'КСБ':
        result = juridical_face_ksb_calculation(spark, id_credit, incident_date, selected_risk, selected_deviation)

    print(f"\nРЕЗУЛЬТАТ: {result}")
    return normalize_result(result, P, selected_borrower, selected_risk, selected_deviation, id_credit)

def normalize_result(result, P, borrower, risk, deviation, id_credit):
    """Приводит разнородный результат (кортеж/скаляр/None) к единой структуре
    для выгрузки в Excel: прямые / косвенные / потенциальные потери."""
    direct = indirect = potential = None
    if isinstance(result, tuple):
        if len(result) >= 1:
            direct = result[0]
        if len(result) >= 2:
            indirect = result[1]
    elif result is not None:
        potential = result
    return {
        "Тип клиента": borrower,
        "ID кредита": id_credit,
        "Дата инцидента": P.get("incident_date"),
        "ЦПР": risk,
        "Отклонение": deviation,
        "Прямые потери": _to_num(direct),
        "Косвенные потери": _to_num(indirect),
        "Потенциальные потери": _to_num(potential),
    }

def _to_num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)

def write_result_excel(result_row, path):
    """Записывает результат расчёта в xlsx (один ряд). Нужно для run_preset -
    рантайм ожидает Excel на выходе ноутбука."""
    df = pd.DataFrame([result_row])
    df.to_excel(path, sheet_name="Отчет_ОпРиски", index=False, engine="openpyxl")
    print(f"Результат сохранён: {path}")
    return df