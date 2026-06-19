---
rule_kind: formElementRule
intent: compute this field's value from earlier observations (e.g. total = sum of
  male + female members)
entity_param: individual
encounter_types: []
concepts:
- Men targeted in camp for 1st dose
- Women targeted in camp for 1st dose
source_org: STC-CV
field_name: Total
kind: value
---
```js
'use strict';
({params, imports}) => {
  const individual = params.entity;
  const formElement = params.formElement;
  const _ = imports.lodash;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({individual, formElement});
  const menPresent = individual.getObservationReadableValue("Men targeted in camp for 1st dose", "18+ Population targeted for 1st dose in camp");
  const womenPresent = individual.getObservationReadableValue("Women targeted in camp for 1st dose", "18+ Population targeted for 1st dose in camp");
  const total = _.toNumber(menPresent || 0) + _.toNumber(womenPresent || 0)
  statusBuilder.value(total);
  return statusBuilder.build();
};
```
