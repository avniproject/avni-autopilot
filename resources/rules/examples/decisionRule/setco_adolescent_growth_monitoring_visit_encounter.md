---
rule_kind: decisionRule
intent: show the subject's BMI category (underweight / normal / overweight / obese)
  based on recorded height and weight
entity_param: individual
encounter_types: []
concepts:
- Height of the Adolescent
- Weight of the Adolescent
source_org: Setco
---
```js
({params, imports}) => {
  const age = programEncounter.programEnrolment.individual.getAgeInYears();
  const weight = programEncounter.getObservationReadableValue('Weight of the Adolescent');
  const height = programEncounter.getObservationReadableValue('Height of the Adolescent');
  const bmi = imports.common.calculateBMI(weight, height);
  // const date = programEncounter.programEnrolment.getEncountersOfType('Adolescent Growth Monitoring Visit')[0].earliestVisitDateTime;
  
  var value = function(x) {
  if (x<=16){
  return 'Severely Undernourished';
  }
  if (x>16 && x<=18) {
  return 'Moderately Undernourished';
  }
  if (x>18 && x<=24){
  return 'Normal';
  }
  if (x>24 && x<=25){
  return 'Over Weight';
  }
  if (x>25){
  return 'Obesity';
  }
  }
  complicationsBuilder
  .addComplication(value(bmi))
  .when.whenItem(bmi)
  .is.lessThan(18)
  .or.whenItem(bmi)
  .is.greaterThanOrEqualTo(24);
  decisions.encounterDecisions.push(complicationsBuilder.getComplications());
  return decisions;
 };
```
