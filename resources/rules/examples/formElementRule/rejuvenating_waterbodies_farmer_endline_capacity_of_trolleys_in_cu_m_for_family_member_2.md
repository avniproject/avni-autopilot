---
rule_kind: formElementRule
intent: show this field only when a numeric value recorded earlier crosses a threshold
  (e.g. show capacity when 'Number of trolleys' is more than 0)
entity_param: encounter
encounter_types: []
concepts: []
source_org: Rejuvenating Waterbodies
field_name: Capacity of trolleys in cu.m. for family member 2
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({encounter, formElement});
  statusBuilder.show().when.valueInEncounter("Number of trolleys carted by family member 2").is.greaterThan(0);
  return statusBuilder.build();
};
```
