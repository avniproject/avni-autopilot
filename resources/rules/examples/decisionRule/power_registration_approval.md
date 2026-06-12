---
rule_kind: decisionRule
intent: surface selected form observations as decisions without transformation
entity_param: encounter
encounter_types: []
concepts:
- Date of Labour Card registration
- Labour Card number
source_org: PoWER
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const decisions = params.decisions;
  const cardNumber = encounter.getObservationReadableValue("Labour Card number");
  const cardRegistrationDate = encounter.getObservationReadableValue("Date of Labour Card registration");
  decisions.registrationDecisions.push({name: "Labour Card number", value: cardNumber});
  decisions.registrationDecisions.push({name: "Date of Labour Card registration", value: cardRegistrationDate});
  return decisions;
 };
```
