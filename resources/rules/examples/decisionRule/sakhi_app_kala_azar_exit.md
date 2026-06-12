---
rule_kind: decisionRule
intent: set the subject's life status to Dead and propagate the date of death to registration
  when a date of death is recorded on exit
entity_param: programEnrolment
encounter_types: []
concepts: []
source_org: Sakhi App
---
```js
"use strict";
 ({params, imports}) => {
  const programEnrolment = params.entity;
  const decisions = params.decisions;
  
  let dateOfDeath = programEnrolment.findExitObservation('ee91d2b0-c93c-4d31-8c48-58738df229ff');
  
  if (dateOfDeath) {
  
  decisions.registrationDecisions.push({name : "Individuals status", value : "Dead"});
  decisions.registrationDecisions.push({name : "Date of death", value: dateOfDeath.getReadableValue() });
  }
  
  return decisions;
 };
```
