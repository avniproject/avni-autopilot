---
rule_kind: formElementRule
intent: hide specific coded answer options for this field based on an earlier answer
  (e.g. hide C-section and Assisted delivery when place of delivery is at home)
entity_param: programEncounter
encounter_types: []
concepts:
- Place of delivery
source_org: JSSCP
field_name: Type of delivery
kind: answerFilter
---
```js
'use strict';
({params, imports}) => {
const programEncounter = params.entity;
const formElement = params.formElement;

const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({programEncounter, formElement});

const place = programEncounter.getObservationReadableValue('Place of delivery');
if(place == "At In law's place" || place == "At mother's place")
statusBuilder.skipAnswers('C-section','Assisted delivery');


return statusBuilder.build();
};
```
