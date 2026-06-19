---
rule_kind: formElementRule
intent: show this field only when an earlier question's answer matches a specific
  option (e.g. show details when 'Consent given' is Yes)
entity_param: individual
encounter_types: []
concepts: []
source_org: Shelter
field_name: Is there any redressal system available for CTBs?
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const individual = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({individual, formElement});
  statusBuilder.show().when.valueInRegistration('Is the CTB in use?').containsAnswerConceptName('Yes');
  return statusBuilder.build();
};
```
