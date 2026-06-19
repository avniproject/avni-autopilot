---
rule_kind: formElementRule
intent: show this field only when several earlier answers together satisfy a combined
  condition (e.g. weight under 2.5 kg AND KMC was done)
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: SNCU
field_name: In last 24 hours for how many times did KMC?
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const programEncounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({programEncounter, formElement});
 statusBuilder.show().when.valueInEncounter('Weight').is.lessThan(2.5).and.when.valueInEncounter('If infant\'s weight is less than 2.5kg then did KMC?').is.yes;
return statusBuilder.build();
};
```
