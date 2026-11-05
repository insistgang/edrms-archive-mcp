from datetime import date


ALLOWED_FIELDS = {'yearFrom', 'yearTo', 'dateField', 'dateFrom', 'dateTo'}


def validate_filters(filters):
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError('filters必须为对象')
    if set(filters) - ALLOWED_FIELDS:
        raise ValueError('filters包含不支持的字段')
    result = dict(filters)
    for field in ('yearFrom', 'yearTo'):
        if field in result:
            value = result[field]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999:
                raise ValueError(field + '必须是1到9999的整数')
    if result.get('yearFrom', 1) > result.get('yearTo', 9999):
        raise ValueError('年度范围倒置')
    has_dates = 'dateFrom' in result or 'dateTo' in result
    if has_dates and result.get('dateField') not in ('formedDate', 'filingDate'):
        raise ValueError('日期范围必须指定dateField：formedDate或filingDate')
    if 'dateField' in result and not has_dates:
        raise ValueError('dateField需要日期范围')
    for field in ('dateFrom', 'dateTo'):
        if field in result:
            value = result[field]
            if not isinstance(value, str):
                raise ValueError(field + '必须是YYYY-MM-DD')
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise ValueError(field + '必须是有效日期') from None
            if parsed.isoformat() != value:
                raise ValueError(field + '必须是YYYY-MM-DD')
    if result.get('dateFrom', '0001-01-01') > result.get('dateTo', '9999-12-31'):
        raise ValueError('日期范围倒置')
    return result


def matches_filters(record, filters):
    year = record.get('year')
    if 'yearFrom' in filters or 'yearTo' in filters:
        if isinstance(year, bool) or not isinstance(year, int):
            return False
        if not filters.get('yearFrom', 1) <= year <= filters.get('yearTo', 9999):
            return False
    if 'dateField' in filters:
        value = record.get('meta', {}).get(filters['dateField'])
        if not isinstance(value, str):
            return False
        try:
            parsed = date.fromisoformat(value).isoformat()
        except ValueError:
            return False
        if not filters.get('dateFrom', '0001-01-01') <= parsed <= filters.get('dateTo', '9999-12-31'):
            return False
    return True
