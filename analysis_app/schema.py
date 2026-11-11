FIELDS = {'observation_id': 'str', 'cohort': 'str', 'channel': 'str', 'start_day': 'int', 'lag': 'int', 'conversions': 'float', 'exposures': 'float', 'reported_day': 'int', 'revision': 'int'}
REQUIRED = ['observation_id', 'cohort', 'channel', 'start_day', 'lag', 'conversions', 'exposures', 'reported_day', 'revision']
ENTITY = 'cohort'
GROUP = 'channel'
MEASURE = 'conversions'
PRODUCT = 'distribution-deconvolution'
UNIT = '次'
